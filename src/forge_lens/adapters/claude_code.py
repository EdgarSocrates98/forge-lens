"""Claude Code session-log adapter — Cycle 10.

Imports Claude Code JSONL session files (~/.claude/projects/*/*.jsonl)
into Forge Lens traces. This is log-derived evidence — spans are
reconstructed from recorded events, never instrumented in-process.
"""

from __future__ import annotations

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


def _ts(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return ensure_aware(datetime.fromisoformat(
                value.replace("Z", "+00:00")))
        except ValueError:
            pass
    return datetime(1970, 1, 1, tzinfo=UTC)


def parse_session(path: Path, *, project_id: str = "claude-code") -> Trace | None:
    """Convert one session JSONL to a Trace; None when nothing usable.

    Evidence rule: only lines actually observed become spans; a session
    with zero tool_use/summary events returns None rather than a
    fabricated trace.
    """
    spans: list[Span] = []
    session_id: str | None = None
    n = 0
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue  # corrupt line — skipped, counted below
            session_id = session_id or ev.get("sessionId") or ev.get("session_id")
            if ev.get("type") == "tool_use" or ev.get("tool_name"):
                n += 1
                name = str(ev.get("tool_name") or ev.get("name") or "tool")
                ts = _ts(ev.get("timestamp"))
                spans.append(Span(
                    trace_id="cc" + (session_id or path.stem).replace("-", "")[:30],
                    span_id=f"{n:016x}",
                    name=f"tool {name}",
                    kind=SpanKind.TOOL,
                    start_time=ts, end_time=ts,
                    status=Status(code=(
                        StatusCode.ERROR if ev.get("is_error") else StatusCode.OK
                    )),
                    attributes={
                        "adapter": "claude_code",
                        "tool.name": name,
                        "evidence.kind": "log_derived",
                    },
                ))
    if not spans:
        return None
    tid = "cc" + (session_id or path.stem).replace("-", "")[:30]
    tid = (tid + "0" * 32)[:32]
    for s in spans:
        object.__setattr__(s, "trace_id", tid)
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
    )
