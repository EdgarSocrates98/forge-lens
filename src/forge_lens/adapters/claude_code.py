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
    calls, results = _extract_tool_calls(events)
    spans: list[Span] = []
    for n, call in enumerate(calls, 1):
        name = str(call.get("name") or "tool")
        ts = call.get("ts")
        is_error, end_ts = (False, None)
        cid = call.get("id")
        if cid is not None and cid in results:
            is_error, end_ts = results[cid]
        elif call.get("is_error"):
            is_error = True
        attrs: dict[str, Any] = {
            "adapter": "claude_code",
            "tool.name": name,
            "evidence.kind": "log_derived",
        }
        if ts is None:
            attrs["integrity.timestamp_fallback"] = True
        if call.get("input") is not None:
            attrs["tool.args"] = call["input"]
        if call.get("usage"):
            # turn-level token counters land on the FIRST tool span of
            # that assistant message — attaching to every one would
            # double-count in cost/efficiency roll-ups
            attrs["forge_lens.token_usage"] = call["usage"]
            attrs["claude.usage_scope"] = "assistant_turn"
        if call.get("model"):
            attrs["gen_ai.request.model"] = call["model"]
        spans.append(Span(
            trace_id=tid,
            span_id=f"{n:016x}",
            name=f"tool {name}",
            kind=SpanKind.TOOL,
            # unparseable ts falls back to epoch AND is flagged —
            # the marker distinguishes it from a real 1970 event
            start_time=ts or _EPOCH, end_time=end_ts or ts or _EPOCH,
            status=Status(code=(
                StatusCode.ERROR if is_error else StatusCode.OK
            )),
            attributes=attrs,
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


def _extract_tool_calls(
    events: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, tuple[bool, Any]]]:
    """Normalize tool calls from BOTH on-disk schemas.

    Real session files (``~/.claude/projects/*/*.jsonl``) emit
    ``{"type":"assistant", "message":{"content":[{"type":"tool_use",
    "id","name","input"}], "usage": {...}, "model": "..."}}`` entries and
    report outcomes in separate ``{"type":"user"}`` entries as
    ``{"type":"tool_result","tool_use_id","is_error"}`` blocks — that's
    where ``is_error`` and the real end timestamp actually live.

    A flat shape (top-level ``tool_use``/``tool_name`` — what hook
    exports produce) is also accepted.

    Returns ``(calls, results)``: calls in file order carrying
    name/id/input/ts/usage/model, results mapping
    ``tool_use_id → (is_error, result_ts)`` for correlation.
    """
    calls: list[dict[str, Any]] = []
    results: dict[str, tuple[bool, Any]] = {}
    for ev in events:
        ts = _ts(ev.get("timestamp"))
        t = ev.get("type")
        if t == "assistant":
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            usage = msg.get("usage") if isinstance(msg, dict) else None
            model = msg.get("model") if isinstance(msg, dict) else None
            if isinstance(content, list):
                first = True
                for c in content:
                    if (isinstance(c, dict)
                            and c.get("type") == "tool_use"):
                        calls.append({
                            "id": c.get("id")
                            if isinstance(c.get("id"), str) else None,
                            "name": c.get("name")
                            if isinstance(c.get("name"), str) else None,
                            "input": c.get("input"),
                            "ts": ts,
                            "usage": usage if first and isinstance(
                                usage, dict) else None,
                            "model": model if first and isinstance(
                                model, str) else None,
                        })
                        first = False
        elif t == "user":
            msg = ev.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for c in content:
                    if (isinstance(c, dict)
                            and c.get("type") == "tool_result"
                            and isinstance(c.get("tool_use_id"), str)):
                        results[c["tool_use_id"]] = (
                            bool(c.get("is_error")), ts)
        # flat shape: top-level tool_use / tool_name (hook exports)
        if t == "tool_use" or isinstance(ev.get("tool_name"), str):
            calls.append({
                "id": ev.get("id") if isinstance(ev.get("id"), str)
                else None,
                "name": ev.get("tool_name") or ev.get("name"),
                "input": ev.get("input"),
                "ts": ts,
                "usage": None,
                "model": None,
                "is_error": bool(ev.get("is_error")),
            })
    return calls, results
