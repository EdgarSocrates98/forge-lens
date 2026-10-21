"""Run evaluator specs across trace sets → a versioned report."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.v1.evaluation import EvaluationResult
from forge_lens.contracts.v1.trace import Trace
from forge_lens.eval.evaluators import run_evaluators


class EvalReport(LensModel):
    """Results of one evaluation run over a named set of traces."""

    dataset: str | None = None
    """Optional name for the evaluated set (e.g. ``"baseline"``)."""
    trace_ids: list[str]
    results: dict[str, list[EvaluationResult]]
    """trace_id → its EvaluationResult list, in spec order."""
    summary: dict[str, dict[str, Any]] = Field(default_factory=dict)
    """``evaluator/metric`` → ``{n, nulls, sum, mean_score, pass_rate}``
    — ``pass_rate`` counts score==1.0 for binary evaluators."""


def evaluate_traces(
    traces: list[Trace],
    specs: list[Any],
    *,
    dataset: str | None = None,
    now: datetime | None = None,
) -> EvalReport:
    """Evaluate ``specs`` on each trace; aggregate per metric.

    Deterministic: traces processed in id order, results keyed by id.
    ``score=None`` results count in ``nulls``, never silently as 0.
    """
    # repeated trace_ids evaluate once — a duplicated id inflates neither
    # results nor summary; trace_ids reports the distinct set evaluated
    ordered = sorted({t.trace_id: t for t in traces}.values(), key=lambda t: t.trace_id)
    results = {
        t.trace_id: run_evaluators(t, specs, now=now) for t in ordered
    }
    acc: dict[str, dict[str, Any]] = {}
    for res_list in results.values():
        for r in res_list:
            key = f"{r.evaluator}/{r.metric}"
            slot = acc.setdefault(
                key, {"n": 0, "sum": 0.0, "passes": 0, "nulls": 0}
            )
            if r.score is None:
                slot["nulls"] += 1
                continue
            slot["n"] += 1
            slot["sum"] += r.score
            if r.score >= 1.0:
                slot["passes"] += 1
    summary = {
        key: {
            "n": s["n"],
            "nulls": s["nulls"],
            "sum": round(s["sum"], 6),
            "mean_score": round(s["sum"] / s["n"], 6) if s["n"] else None,
            "pass_rate": round(s["passes"] / s["n"], 6) if s["n"] else None,
        }
        for key, s in sorted(acc.items())
    }
    return EvalReport(
        dataset=dataset,
        trace_ids=[t.trace_id for t in ordered],
        results=results,
        summary=summary,
    )
