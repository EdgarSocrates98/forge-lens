"""Smart recommendations — Cycle 12 §12.4.

Rules produce *recommendations* with the evidence that fired them.
Every claim is labelled 'candidate' — gains require experimental
validation (the prompt's own rule), which /v1/eval/compare provides.
"""

from __future__ import annotations

from typing import Any

from forge_lens.contracts.v1.trace import SpanKind, Trace
from forge_lens.intel.cost import extract_usage
from forge_lens.ops.loops import detect_loops


def recommend(trace: Trace) -> dict[str, Any]:
    """Deterministic recommendation pass over one trace."""
    recs: list[dict[str, Any]] = []
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    errs = [s for s in tools if s.status.code.value == "error"]

    loops = detect_loops(trace)["findings"]
    for f in loops:
        if f["kind"] == "repeated_identical_tool":
            recs.append({
                "kind": "redundant_tool_calls",
                "classification": "candidate",
                "spans": f["span_ids"],
                "detail": "identical tool+args repeated — consider caching"
                          " or deduplicating; validate via A/B before"
                          " claiming gains",
            })

    by_name: dict[str, int] = {}
    for s in errs:
        by_name[s.name] = by_name.get(s.name, 0) + 1
    for name in sorted(by_name):
        if by_name[name] >= 2:
            recs.append({
                "kind": "unstable_tool",
                "classification": "candidate",
                "tool": name,
                "failures": by_name[name],
                "detail": f"tool {name!r} failed {by_name[name]}x — flag for"
                          " retry policy or replacement review",
            })

    llms = [s for s in trace.spans if s.kind is SpanKind.LLM]
    big_ctx = []
    for s in llms:
        u = extract_usage(s)
        if u is not None and (u.input_tokens or 0) >= 100_000:
            big_ctx.append(s.span_id)
    if big_ctx:
        recs.append({
            "kind": "context_budget",
            "classification": "candidate",
            "spans": sorted(big_ctx),
            "detail": "LLM calls with >=100k input tokens — retrieval reuse"
                      " or summarization may cut cost; needs experiment",
        })

    if len(tools) >= 30:
        recs.append({
            "kind": "tool_call_volume",
            "classification": "candidate",
            "tool_calls": len(tools),
            "detail": "high tool-call volume — delegation or batching may"
                      " help; measure before adopting",
        })
    recs.sort(key=lambda r: (r["kind"], r.get("tool") or ""))
    return {"trace_id": trace.trace_id, "recommendations": recs,
            "note": "candidate recommendations — gains require"
                    " experimental validation, none are claimed"}
