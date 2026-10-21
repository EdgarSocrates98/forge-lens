"""Loop & no-progress detection — Cycle 07 §07.3/§07.4.

Deterministic detectors over span sequences. Every finding is marked
``heuristic`` — repetition *can* be legitimate (polling, batch work);
we surface evidence, never auto-condemn.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from forge_lens.contracts.v1.trace import Span, SpanKind, Trace


def _signature(span_name: str, attrs: dict[str, Any]) -> str:
    """Stable signature of a tool call — name + args, content-hashed."""
    args = attrs.get("tool.args") or attrs.get("gen_ai.tool.call.arguments") or {}
    digest = hashlib.sha256(
        json.dumps(args, sort_keys=True, default=str).encode()
    ).hexdigest()[:12]
    return f"{span_name}:{digest}"


def detect_loops(trace: Trace, *, repeat_threshold: int = 3) -> dict[str, Any]:
    """Detect repeated identical tool calls, retry runs, and no-progress
    patterns. ``repeat_threshold`` is the minimum run length flagged."""
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    tools.sort(key=lambda s: (s.start_time, s.span_id))
    findings: list[dict[str, Any]] = []

    # 1. runs of the *identical* signature (same tool, same args)
    runs: list[list[str]] = []
    cur: list[str] = []
    last_sig = None
    for s in tools:
        sig = _signature(s.name, s.attributes)
        if sig == last_sig:
            cur.append(s.span_id)
        else:
            if len(cur) >= repeat_threshold:
                runs.append(cur)
            cur = [s.span_id]
            last_sig = sig
    if len(cur) >= repeat_threshold:
        runs.append(cur)
    for run in sorted(runs, key=lambda r: (-len(r), r[0])):
        findings.append({
            "kind": "repeated_identical_tool",
            "classification": "heuristic",
            "span_ids": run,
            "count": len(run),
            "detail": "same tool called with identical arguments — a loop"
                      " unless the repetition is intentional polling",
        })

    # 2. retry loops: same tool name, changing args, all error
    by_name: dict[str, list[Span]] = {}
    for s in tools:
        by_name.setdefault(s.name, []).append(s)
    for name in sorted(by_name):
        group = by_name[name]
        if len(group) >= repeat_threshold and all(
            s.status.code.value == "error" for s in group
        ):
            findings.append({
                "kind": "retry_loop",
                "classification": "heuristic",
                "span_ids": [s.span_id for s in group],
                "count": len(group),
                "detail": f"tool {name!r} failed {len(group)}x in one trace",
            })

    # 3. cyclic delegation: agent spans re-entering the same name
    agents = [s for s in trace.spans if s.kind is SpanKind.AGENT]
    agent_names: dict[str, list[str]] = {}
    for s in agents:
        agent_names.setdefault(s.name, []).append(s.span_id)
    for name in sorted(agent_names):
        ids = agent_names[name]
        if len(ids) >= repeat_threshold:
            findings.append({
                "kind": "repeated_agent_span",
                "classification": "heuristic",
                "span_ids": ids,
                "count": len(ids),
                "detail": f"agent span {name!r} re-entered {len(ids)}x —"
                          " possible cyclic delegation",
            })

    # 4. no-progress: repetition + errors — only when errored spans
    # actually exist; an empty evidence list is not a finding
    errored = [s for s in tools if s.status.code.value == "error"]
    if findings and errored:
        findings.append({
            "kind": "no_progress_signal",
            "classification": "heuristic",
            "span_ids": [s.span_id for s in errored],
            "count": len(errored),
            "detail": "repetition co-occurs with failures — progress is"
                      " unproven, not disproven",
        })
    findings.sort(key=lambda f: (f["kind"], f["span_ids"][0] if f["span_ids"] else ""))
    return {"trace_id": trace.trace_id, "findings": findings,
            "classification": "heuristic",
            "note": "repetition is evidence, not a verdict — legitimate"
                    " polling/batching looks identical"}
