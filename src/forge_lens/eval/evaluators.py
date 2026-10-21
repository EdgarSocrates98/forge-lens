"""Built-in evaluators — pure functions over a stored Trace.

Every evaluator returns :class:`EvaluationResult` with ``derived``
provenance and the spans/values it used in ``details``. ``binary``
marks pass/fail metrics eligible for the proportion test in
:mod:`forge_lens.eval.compare`; continuous metrics report deltas only.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime
from typing import Any

from forge_lens.contracts.v1.evaluation import EvaluationResult
from forge_lens.contracts.v1.trace import SpanKind, Trace
from forge_lens.intel.cost import extract_usage

Evaluator = Callable[[Trace, dict[str, Any], datetime | None], EvaluationResult]

_EVALUATOR_VERSION = "1.0.0"


def _result(
    name: str,
    metric: str,
    score: float | None,
    trace: Trace,
    now: datetime | None,
    details: dict[str, str],
) -> EvaluationResult:
    return EvaluationResult(
        evaluator=name,
        evaluator_version=_EVALUATOR_VERSION,
        metric=metric,
        score=score,
        trace_reference=trace.trace_id,
        evaluated_at=now,
        details=details,
    )


def _error_free(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    errs = [s.span_id for s in trace.spans if s.status.code.value == "error"]
    return _result(
        "error_free",
        "no_error_spans",
        0.0 if errs else 1.0,
        trace,
        now,
        {"error_span_ids": ",".join(errs), "error_count": str(len(errs))},
    )


def _latency_budget(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    budget = float(params["budget_ms"])
    if not math.isfinite(budget):
        raise ValueError("budget_ms must be finite")
    if trace.ended_at is None or not trace.spans:
        return _result(
            "latency_budget",
            "within_budget_ms",
            None,
            trace,
            now,
            {"budget_ms": str(budget), "reason": "trace incomplete — no verdict"},
        )
    # envelope wall-clock — the honest "how long did the run take"
    observed = (trace.ended_at - trace.started_at).total_seconds() * 1000.0
    if observed < 0:
        return _result(
            "latency_budget",
            "within_budget_ms",
            None,
            trace,
            now,
            {
                "budget_ms": str(budget),
                "observed_ms": f"{observed:.3f}",
                "reason": "negative envelope duration — no verdict",
            },
        )
    return _result(
        "latency_budget",
        "within_budget_ms",
        1.0 if observed <= budget else 0.0,
        trace,
        now,
        {"observed_ms": f"{observed:.3f}", "budget_ms": str(budget)},
    )


def _token_budget(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    raw_budget = params["max_total_tokens"]
    # bool is a subclass of int — True would silently become budget 1;
    # strings parse fine through int() but a wire string is not a
    # number the caller verified. Both are invalid params → no verdict.
    if isinstance(raw_budget, (bool, str)):
        raise TypeError(
            f"max_total_tokens must be a number, got {type(raw_budget).__name__}"
        )
    if isinstance(raw_budget, float) and not math.isfinite(raw_budget):
        raise ValueError("max_total_tokens must be finite")
    budget = int(raw_budget)
    total = 0
    known = 0
    for s in trace.spans:
        u = extract_usage(s)
        if u is not None:
            known += 1
            # same convention as trace_cost — `or`, not `is not None`:
            # a producer reporting only the aggregate still counts, and a
            # contradictory `total_tokens=0` with nonzero parts counts the
            # parts (over-counting is the safe direction for budgets)
            total += u.total_tokens or (
                (u.input_tokens or 0) + (u.output_tokens or 0)
            )
    unknown = len(trace.spans) - known
    details = {
        "total_tokens": str(total),
        "budget": str(budget),
        "spans_with_usage": str(known),
    }
    score: float | None
    if unknown:
        details["spans_without_usage"] = str(unknown)
        # score on observed tokens; the gap is evidence, not zero
        details["caveat"] = "unobserved spans may add tokens"
    score = 1.0 if total <= budget else 0.0
    return _result("token_budget", "within_token_budget", score, trace, now, details)


def _tool_success(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    if not tools:
        return _result(
            "tool_success", "tool_success_rate", None, trace, now,
            {"reason": "no tool spans in trace"},
        )
    bad = [s.span_id for s in tools if s.status.code.value == "error"]
    rate = (len(tools) - len(bad)) / len(tools)
    return _result(
        "tool_success",
        "tool_success_rate",
        round(rate, 6),
        trace,
        now,
        {
            "tool_spans": str(len(tools)),
            "failed": ",".join(bad),
        },
    )


def _has_span(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    name = params.get("name")
    kind = params.get("kind")
    hits = [
        s for s in trace.spans
        if (name is None or s.name == name)
        and (kind is None or s.kind.value == kind)
    ]
    return _result(
        "has_span",
        "expected_span_present",
        1.0 if hits else 0.0,
        trace,
        now,
        {
            "name": str(name or "*"),
            "kind": str(kind or "*"),
            "matched": ",".join(s.span_id for s in hits),
        },
    )


# name → (fn, binary?) — binary marks metrics whose trace-level pass
# fraction (score==1.0 counts) supports the proportion z-test in compare
EVALUATORS: dict[str, tuple[Evaluator, bool]] = {
    "error_free": (_error_free, True),
    "latency_budget": (_latency_budget, True),
    "token_budget": (_token_budget, True),
    "tool_success": (_tool_success, True),
    "has_span": (_has_span, True),
}
"""Binary flag: ``True`` = the score is a per-trace pass (1.0)/fail —
the pass fraction across traces is a proportion the z-test can test."""


def run_evaluators(
    trace: Trace,
    specs: list[Any],
    *,
    now: datetime | None = None,
) -> list[EvaluationResult]:
    """Run named evaluators with params on one trace.

    Unknown evaluator names produce a scored-``None`` result naming the
    miss — an eval run never crashes on a typo'd spec, it reports it.
    ``now`` is injected: the engine never reads a clock.
    """
    out: list[EvaluationResult] = []
    for spec in specs:
        if not isinstance(spec, dict):
            # a malformed spec reports itself — it never 500s the run
            out.append(
                _result(
                    "invalid_spec",
                    "evaluator_error",
                    None,
                    trace,
                    now,
                    {"reason": f"spec must be an object, got {type(spec).__name__}"},
                )
            )
            continue
        name = str(spec.get("name", ""))
        entry = EVALUATORS.get(name)
        params = spec.get("params") or {}
        if entry is None:
            out.append(
                _result(
                    name or "unknown",
                    "unknown_evaluator",
                    None,
                    trace,
                    now,
                    {"reason": f"unknown evaluator {name!r}"},
                )
            )
            continue
        if not isinstance(params, dict):
            out.append(
                _result(
                    name,
                    "evaluator_error",
                    None,
                    trace,
                    now,
                    {"reason": f"params must be an object, got {type(params).__name__}"},
                )
            )
            continue
        fn, _binary = entry
        try:
            out.append(fn(trace, params, now))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            out.append(
                _result(
                    name,
                    "evaluator_error",
                    None,
                    trace,
                    now,
                    {"reason": f"missing/invalid params: {exc}"},
                )
            )
    return out
