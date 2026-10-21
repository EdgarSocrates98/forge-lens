"""Claude Code session-log adapter — Cycle 10.

Imports Claude Code JSONL session files (~/.claude/projects/*/*.jsonl)
into Forge Lens traces. This is log-derived evidence — spans are
reconstructed from recorded events, never instrumented in-process.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
    TraceSource,
)
from forge_lens.core.time import ensure_aware


def _hex32(raw: Any) -> str:
    """Deterministic hex trace_id from any session identifier — file
    names and non-hex ids are hashed, never pattern-violating."""
    return hashlib.sha256(str(raw).encode()).hexdigest()[:32]


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# bounded read: a session file is a local artifact, not a
# stream — refuse pathological sizes rather than OOM
_MAX_SESSION_BYTES = 256 * 1024 * 1024


def _ts(value: Any) -> datetime | None:
    """Parsed timestamp or None — callers mark the fallback explicitly
    rather than letting an epoch value pose as a real observation."""
    if isinstance(value, str):
        try:
            return ensure_aware(datetime.fromisoformat(
                value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return None


def parse_session(path: Path, *, project_id: str = "claude-code") -> Trace | None:
    """Convert one session JSONL to a Trace; None when nothing usable.

    Evidence rule: only lines actually observed become spans; a session
    with zero tool_use/summary events returns None rather than a
    fabricated trace.
    """
    if path.stat().st_size > _MAX_SESSION_BYTES:
        raise ValueError(
            f"session file exceeds {_MAX_SESSION_BYTES // (1 << 20)} MiB — "
            "split it or export a range")
    events: list[dict[str, Any]] = []
    session_id: str | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # corrupt line — skipped, not fatal
            if not isinstance(ev, dict):
                continue
            events.append(ev)
            if session_id is None:
                cand = ev.get("sessionId") or ev.get("session_id")
                if isinstance(cand, str) and cand:
                    session_id = cand
    tid = _hex32(session_id or path.stem)  # resolved ONCE — every span shares it
    spans: list[Span] = []
    for n, ev in enumerate(events, 1):
        if ev.get("type") == "tool_use" or ev.get("tool_name"):
            name = str(ev.get("tool_name") or ev.get("name") or "tool")
            ts = _ts(ev.get("timestamp"))
            spans.append(Span(
                trace_id=tid,
                span_id=f"{n:016x}",
                name=f"tool {name}",
                kind=SpanKind.TOOL,
                # unparseable ts falls back to epoch AND is flagged —
                # the marker distinguishes it from a real 1970 event
                start_time=ts or _EPOCH, end_time=ts or _EPOCH,
                status=Status(code=(
                    StatusCode.ERROR if ev.get("is_error") else StatusCode.OK
                )),
                attributes={
                    "adapter": "claude_code",
                    "tool.name": name,
                    "evidence.kind": "log_derived",
                    **({"integrity.timestamp_fallback": True}
                       if ts is None else {}),
                },
            ))
    if not spans:
        return None
    return Trace(
        trace_id=tid,
        root_span_id=spans[0].span_id,
        spans=spans,
        started_at=spans[0].start_time,
        ended_at=spans[-1].end_time,
        status=Status(code=(
            StatusCode.ERROR
            if any(s.status.code is StatusCode.ERROR for s in spans)
            else StatusCode.OK
        )),
        source=TraceSource(system="claude_code", sdk_name="jsonl-adapter"),
        session_id=session_id,
        project_id=project_id,
    )
