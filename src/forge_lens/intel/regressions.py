"""Cross-version regression comparison — Cycle 12.

Compares efficiency/cost metrics between two trace cohorts labelled by
any dimension (agent/prompt/skill/model/tool version). Deltas are
observed differences; significance is NOT claimed without the eval
module's statistical machinery.
"""

from __future__ import annotations

from typing import Any

from forge_lens.contracts.v1.trace import Trace
from forge_lens.intel.efficiency import fleet_efficiency, trace_efficiency


def compare_cohorts(
    base: list[Trace],
    candidate: list[Trace],
    *,
    label: str = "cohort",
) -> dict[str, Any]:
    """Observed deltas between cohorts — honest 'difference observed',
    never 'statistically significant' (that language belongs to
    eval.compare's z-test machinery)."""
    a = fleet_efficiency(base)
    b = fleet_efficiency(candidate)
    metrics = (
        "success_rate", "tokens_per_trace", "tokens_per_success",
        "tool_calls_per_trace", "tool_calls_per_success",
        "retry_overhead", "error_rate",
    )
    deltas: dict[str, Any] = {}
    for m in metrics:
        va, vb = a.get(m), b.get(m)
        if va is None or vb is None:
            deltas[m] = {"base": va, "candidate": vb,
                         "delta": None, "reason": "signal absent"}
        else:
            deltas[m] = {
                "base": va, "candidate": vb,
                "delta": vb - va,
                "pct": ((vb - va) / abs(va) * 100) if va else None,
            }
    return {
        "label": label,
        "base_traces": a["traces"], "candidate_traces": b["traces"],
        "deltas": deltas,
        "classification": "observed_difference",
        "note": "deltas are observed, not significance-tested — use"
                " /v1/eval/compare for the statistical verdict",
    }


def per_trace_regressions(
    base: list[Trace], candidate: list[Trace],
) -> dict[str, Any]:
    """Per-trace diff of efficiency rows (sorted, evidence-first)."""
    a = sorted((trace_efficiency(t) for t in base), key=lambda r: r["trace_id"])
    b = sorted((trace_efficiency(t) for t in candidate), key=lambda r: r["trace_id"])
    return {"base": a, "candidate": b}
