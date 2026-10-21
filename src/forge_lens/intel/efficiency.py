"""Efficiency intelligence — Cycle 12.

Cost per success, tools-per-outcome, retry overhead, evidence coverage.
All functions are pure over stored evidence; a missing signal yields
``None`` + a reason, never an invented number.
"""

from __future__ import annotations

from typing import Any

from forge_lens.contracts.v1.trace import SpanKind, Trace
from forge_lens.intel.cost import extract_usage


def trace_efficiency(trace: Trace) -> dict[str, Any]:
    """Per-trace efficiency roll-up from observed spans only."""
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    llms = [s for s in trace.spans if s.kind is SpanKind.LLM]
    errors = [s for s in trace.spans if s.status.code.value == "error"]
    in_toks = out_toks = 0
    seen_usage = 0
    for s in llms:
        u = extract_usage(s)
        if u is not None:
            seen_usage += 1
            in_toks += u.input_tokens or 0
            out_toks += u.output_tokens or 0
    retries = 0
    for s in trace.spans:
        r = s.attributes.get("gen_ai.tool.call.retry_count") or \
            s.attributes.get("retry.count") or 0
        try:
            retries += int(r)
        except (TypeError, ValueError):
            continue
    return {
        "trace_id": trace.trace_id,
        "outcome": trace.status.code.value,
        "tool_calls": len(tools),
        "llm_calls": len(llms),
        "error_spans": len(errors),
        "retries_observed": retries,
        "input_tokens": in_toks,
        "output_tokens": out_toks,
        "usage_coverage": f"{seen_usage}/{len(llms)} llm spans",
        "succeeded": trace.status.code.value == "ok",
    }


def fleet_efficiency(traces: list[Trace]) -> dict[str, Any]:
    """Aggregates over a trace set — the 'per successful task' metrics."""
    rows = [trace_efficiency(t) for t in traces]
    ok = [r for r in rows if r["succeeded"]]
    n = len(rows)
    tok_in = sum(r["input_tokens"] for r in rows)
    tok_out = sum(r["output_tokens"] for r in rows)
    tool_calls = sum(r["tool_calls"] for r in rows)
    retries = sum(r["retries_observed"] for r in rows)
    return {
        "traces": n,
        "succeeded": len(ok),
        "success_rate": (len(ok) / n) if n else None,
        "tokens_per_trace": (tok_in + tok_out) / n if n else None,
        "tokens_per_success": ((tok_in + tok_out) / len(ok)) if ok else None,
        "tool_calls_per_trace": tool_calls / n if n else None,
        "tool_calls_per_success": tool_calls / len(ok) if ok else None,
        "retry_overhead": retries / n if n else None,
        "error_rate": (
            sum(r["error_spans"] for r in rows) /
            max(1, sum(r["tool_calls"] + r["llm_calls"] for r in rows))
        ) if n else None,
        "note": "None = signal absent, not zero",
    }
