"""Recorded-execution replay (Cycle 05 backend).

Local-first honesty: this *re-walks* the recorded evidence — it cannot
re-execute agent code (the host process isn't here). The timeline is
deterministic: events ordered by ``(timestamp, span_id, kind)``.
"""

from __future__ import annotations

from typing import Any

from forge_lens.contracts.v1.trace import Trace


def replay_timeline(trace: Trace) -> dict[str, Any]:
    """Interleave span start/end and span events into one timeline.

    Each entry: ``{t, span_id, name, kind: span_start|span_end|event,
    ...}`` — offsets in ms relative to the earliest observed timestamp
    make the replay comparable across machines.
    """
    entries: list[dict[str, Any]] = []
    for s in sorted(trace.spans, key=lambda s: (s.start_time, s.span_id)):
        entries.append(
            {
                "t": s.start_time.isoformat(),
                "_sort": s.start_time,
                "span_id": s.span_id,
                "name": s.name,
                "kind": "span_start",
            }
        )
        for ev in s.events:
            entries.append(
                {
                    "t": ev.time.isoformat(),
                    "_sort": ev.time,
                    "span_id": s.span_id,
                    "name": s.name,
                    "kind": "event",
                    "event": ev.name,
                }
            )
        if s.end_time is not None:
            entries.append(
                {
                    "t": s.end_time.isoformat(),
                    "_sort": s.end_time,
                    "span_id": s.span_id,
                    "name": s.name,
                    "kind": "span_end",
                    "status": s.status.code.value,
                }
            )
    entries.sort(key=lambda e: (e["_sort"], e["span_id"], e["kind"]))
    if not entries:
        return {
            "trace_id": trace.trace_id,
            "entries": [],
            "span_count": 0,
            "total_ms": 0.0,
            "unclosed_spans": 0,
        }

    t0 = entries[0]["_sort"]
    total_ms = (entries[-1]["_sort"] - t0).total_seconds() * 1000.0
    for e in entries:
        e["offset_ms"] = round(
            (e["_sort"] - t0).total_seconds() * 1000.0, 3
        )
        del e["_sort"]
    return {
        "trace_id": trace.trace_id,
        "entries": entries,
        "span_count": len(trace.spans),
        "total_ms": round(total_ms, 3),
        "unclosed_spans": sum(1 for s in trace.spans if s.end_time is None),
    }
