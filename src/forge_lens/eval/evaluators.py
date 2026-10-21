"""Built-in evaluators — pure functions over a stored Trace.

Every evaluator returns :class:`EvaluationResult` with ``derived``
provenance and the spans/values it used in ``details``. ``binary``
marks pass/fail metrics eligible for the proportion test in
:mod:`forge_lens.eval.compare`; continuous metrics report deltas only.
"""

from __future__ import annotations

import math
import re
from collections import Counter
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
    # `max_duration_ms` is the canonical name; `budget_ms` stays accepted
    # — it is what the CLI (`--budget-ms`) and pre-existing specs emit
    if "max_duration_ms" in params:
        raw = params["max_duration_ms"]
    elif "budget_ms" in params:
        raw = params["budget_ms"]
    else:
        raise KeyError("max_duration_ms")
    # bool is a subclass of int and strings parse through float() — a
    # wire value the caller typed as a number is the only valid shape
    # (same convention as token_budget)
    if isinstance(raw, (bool, str)) or not isinstance(raw, (int, float)):
        raise TypeError(
            f"max_duration_ms must be a number, got {type(raw).__name__}"
        )
    budget = float(raw)
    if not math.isfinite(budget):
        raise ValueError("max_duration_ms must be finite")
    if budget <= 0:
        raise ValueError("max_duration_ms must be positive")
    if trace.ended_at is None:
        return _result(
            "latency_budget",
            "within_budget_ms",
            None,
            trace,
            now,
            {
                "budget_ms": str(budget),
                "reason": "unended trace — duration unknown",
            },
        )
    if not trace.spans:
        return _result(
            "latency_budget",
            "within_budget_ms",
            None,
            trace,
            now,
            {
                "budget_ms": str(budget),
                "reason": "empty trace — duration unknown",
            },
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


def _observed_value(trace: Trace, field: str) -> tuple[Any, str | None]:
    """Resolve ``field`` to the first span attribute carrying it.

    Returns ``(value, span_id)``; ``(None, None)`` when unobserved —
    an absent field is evidence, never an implicit match."""
    for s in trace.spans:
        if field in s.attributes:
            return s.attributes[field], s.span_id
    if field in trace.resource_attributes:
        return trace.resource_attributes[field], None
    return None, None


def _exact_match(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    field = str(params["field"])
    expected = params.get("expected")
    observed, span_id = _observed_value(trace, field)
    if span_id is None and observed is None:
        return _result(
            "exact_match",
            "field_exact_match",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed in trace"},
        )
    matched = observed == expected
    return _result(
        "exact_match",
        "field_exact_match",
        1.0 if matched else 0.0,
        trace,
        now,
        {
            "field": field,
            "expected": repr(expected),
            "observed": repr(observed),
            "observed_in": span_id or "resource_attributes",
        },
    )


_WORD_RE = re.compile(r"[a-z0-9]+")


def _token_f1(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Bag-of-tokens F1 between an expected string and an observed field.

    Continuous metric — precision/recall when the expected answer is
    free text; never a binary verdict on approximate match."""
    field = str(params["field"])
    expected = params.get("expected")
    if not isinstance(expected, str):
        raise TypeError("token_f1 'expected' must be a string")
    observed, span_id = _observed_value(trace, field)
    if not isinstance(observed, str):
        return _result(
            "token_f1",
            "field_token_f1",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed as string"},
        )
    exp = Counter(_WORD_RE.findall(expected.lower()))
    obs = Counter(_WORD_RE.findall(observed.lower()))
    if not exp and not obs:
        return _result("token_f1", "field_token_f1", 1.0, trace, now,
                       {"field": field, "reason": "both empty"})
    common = sum((exp & obs).values())
    precision = common / sum(obs.values()) if obs else 0.0
    recall = common / sum(exp.values()) if exp else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return _result(
        "token_f1",
        "field_token_f1",
        round(f1, 6),
        trace,
        now,
        {
            "field": field,
            "precision": f"{precision:.6f}",
            "recall": f"{recall:.6f}",
            "observed_in": span_id or "resource_attributes",
        },
    )


def _allowed_tools(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Tool-use validation — every TOOL span name ⊆ ``allowed``."""
    allowed = params.get("allowed")
    if not isinstance(allowed, list) or not all(
        isinstance(t, str) for t in allowed
    ):
        raise TypeError("allowed must be a list of tool names")
    allowed_set = set(allowed)
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    # semantic tool name: `tool.name` attribute when observed, else the
    # span name — both are evidence, never guessed
    violations = sorted(
        {
            str(s.attributes.get("tool.name") or s.name)
            for s in tools
            if str(s.attributes.get("tool.name") or s.name) not in allowed_set
        }
    )
    return _result(
        "allowed_tools",
        "tools_within_allowlist",
        1.0 if not violations else 0.0,
        trace,
        now,
        {
            "tool_spans": str(len(tools)),
            "allowed": ",".join(sorted(allowed_set)),
            "violations": ",".join(violations),
        },
    )


def _task_completion(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Task completion — trace finished and (optionally) without error."""
    # non-bool (e.g. the string "false", which is truthy) falls back to
    # the safe default rather than silently inverting the check
    _rne = params.get("require_no_error", True)
    require_no_error = _rne if isinstance(_rne, bool) else True
    if trace.ended_at is None:
        return _result(
            "task_completion",
            "task_completed",
            0.0,
            trace,
            now,
            {"reason": "trace has no end time — still in flight"},
        )
    errors = [s.span_id for s in trace.spans if s.status.code.value == "error"]
    failed = bool(errors) and require_no_error
    return _result(
        "task_completion",
        "task_completed",
        0.0 if failed else 1.0,
        trace,
        now,
        {
            "ended": "true",
            "error_spans": ",".join(errors),
            "require_no_error": str(bool(require_no_error)).lower(),
        },
    )


def _bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    """Optional bool — a non-bool falls back to ``default`` instead of
    silently flipping semantics (same convention as task_completion)."""
    raw = params.get(key, default)
    return raw if isinstance(raw, bool) else default


def _output_field(params: dict[str, Any]) -> str:
    """Field carrying the trace's final output — ``output.value`` is the
    OpenInference convention; ``field`` overrides for other producers."""
    return str(params.get("field") or "output.value")


def _contains(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Literal-substring check against the trace's observed output."""
    text = params["text"]
    if not isinstance(text, str):
        raise TypeError("contains 'text' must be a string")
    field = _output_field(params)
    case_sensitive = _bool_param(params, "case_sensitive", True)
    observed, span_id = _observed_value(trace, field)
    if not isinstance(observed, str):
        return _result(
            "contains",
            "output_contains",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed as string"},
        )
    haystack = observed if case_sensitive else observed.lower()
    needle = text if case_sensitive else text.lower()
    matched = needle in haystack
    return _result(
        "contains",
        "output_contains",
        1.0 if matched else 0.0,
        trace,
        now,
        {
            "field": field,
            "matched": str(matched).lower(),
            "observed_in": span_id or "resource_attributes",
            "case_sensitive": str(case_sensitive).lower(),
            "needle": text[:120],
        },
    )


def _regex(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Regex search against the trace's observed output — an invalid
    pattern is a malformed spec (evaluator_error), never a crash."""
    pattern = params["pattern"]
    if not isinstance(pattern, str):
        raise TypeError("regex 'pattern' must be a string")
    case_sensitive = _bool_param(params, "case_sensitive", True)
    try:
        rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        # re.error is not a ValueError — re-raise as one so the runner's
        # evaluator_error path reports it instead of letting it escape
        raise ValueError(f"invalid pattern: {exc}") from exc
    field = _output_field(params)
    observed, span_id = _observed_value(trace, field)
    if not isinstance(observed, str):
        return _result(
            "regex",
            "output_regex_match",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed as string"},
        )
    m = rx.search(observed)
    details = {
        "field": field,
        "pattern": pattern,
        "matched": str(m is not None).lower(),
        "observed_in": span_id or "resource_attributes",
        "case_sensitive": str(case_sensitive).lower(),
    }
    if m is not None:
        match = m.group(0)
        details["match"] = match[:120] + ("…" if len(match) > 120 else "")
    return _result(
        "regex",
        "output_regex_match",
        1.0 if m is not None else 0.0,
        trace,
        now,
        details,
    )


def _count_bound(params: dict[str, Any], key: str) -> int:
    """``min``/``max`` bound — a non-negative integer; bools, strings and
    fractional floats are invalid params, not silent truncations."""
    raw = params[key]  # KeyError → evaluator_error
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{key} must be an integer, got {type(raw).__name__}")
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        raise ValueError(f"{key} must be a finite integer")
    bound = int(raw)
    if bound < 0:
        raise ValueError(f"{key} must be ≥ 0")
    return bound


def _tool_call_count(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Count TOOL-kind spans (optionally one ``tool.name``) — pass when
    the observed count sits inside the inclusive [min, max] bounds."""
    lo = _count_bound(params, "min")
    hi = _count_bound(params, "max")
    if lo > hi:
        raise ValueError(f"min ({lo}) must be ≤ max ({hi})")
    tool = params.get("tool")
    if tool is not None and not isinstance(tool, str):
        raise TypeError(f"tool must be a string, got {type(tool).__name__}")
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    counted = (
        tools
        if tool is None
        else [s for s in tools if s.attributes.get("tool.name") == tool]
    )
    count = len(counted)
    return _result(
        "tool_call_count",
        "tool_calls_within_bounds",
        1.0 if lo <= count <= hi else 0.0,
        trace,
        now,
        {
            "tool": tool if tool is not None else "*",
            "count": str(count),
            "min": str(lo),
            "max": str(hi),
            "span_ids": ",".join(s.span_id for s in counted),
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
    "exact_match": (_exact_match, True),
    "token_f1": (_token_f1, False),  # continuous — deltas only
    "allowed_tools": (_allowed_tools, True),
    "task_completion": (_task_completion, True),
    "contains": (_contains, True),
    "regex": (_regex, True),
    "tool_call_count": (_tool_call_count, True),
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
