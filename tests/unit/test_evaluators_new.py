"""Unit tests for the contains/regex/latency_budget/tool_call_count
evaluators — every verdict is evidence-backed and every malformed spec
degrades to ``evaluator_error`` (score=None), never a raised exception.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from forge_lens.contracts.v1.evaluation import EvaluationResult
from forge_lens.contracts.v1.trace import Span, SpanKind, Trace
from forge_lens.eval import run_evaluators

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TRACE_ID = "b" * 32
OUTPUT = "The capital of France is Paris."


def _span(
    sid: str,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    dur_ms: float = 100.0,
    attrs: dict[str, Any] | None = None,
) -> Span:
    return Span(
        trace_id=TRACE_ID,
        span_id=sid.ljust(16, "0")[:16],
        name=name,
        kind=kind,
        start_time=T0,
        end_time=T0 + timedelta(milliseconds=dur_ms),
        attributes=attrs or {},
    )


def _trace(spans: list[Span], *, ended: bool = True) -> Trace:
    return Trace(
        trace_id=TRACE_ID,
        project_id="p1",
        started_at=T0,
        ended_at=T0 + timedelta(seconds=10) if ended else None,
        spans=spans,
    )


def _output_trace() -> Trace:
    """Trace whose LLM span carries the conventional output field."""
    return _trace(
        [
            _span("a1", "agent", kind=SpanKind.AGENT),
            _span(
                "b1",
                "llm",
                kind=SpanKind.LLM,
                attrs={"output.value": OUTPUT},
            ),
        ]
    )


def _run(
    trace: Trace, name: str, params: Any = None
) -> EvaluationResult:
    # run_evaluators normalizes a missing/None params to {} — passing it
    # through exercises the same spec path the API exposes
    return run_evaluators(trace, [{"name": name, "params": params}])[0]


# -- contains -----------------------------------------------------------------


def test_contains_match_and_miss() -> None:
    t = _output_trace()
    hit = _run(t, "contains", {"text": "Paris"})
    assert hit.score == 1.0
    assert hit.evaluator == "contains"
    assert hit.metric == "output_contains"
    assert hit.details["matched"] == "true"
    assert hit.details["observed_in"] == "b1".ljust(16, "0")
    assert hit.details["field"] == "output.value"

    miss = _run(t, "contains", {"text": "London"})
    assert miss.score == 0.0
    assert miss.details["matched"] == "false"
    assert miss.details["observed_in"] == "b1".ljust(16, "0")


def test_contains_case_sensitivity() -> None:
    t = _output_trace()
    # default is case-sensitive — a lowercase needle misses
    assert _run(t, "contains", {"text": "paris"}).score == 0.0
    insensitive = _run(
        t, "contains", {"text": "paris", "case_sensitive": False}
    )
    assert insensitive.score == 1.0
    assert insensitive.details["case_sensitive"] == "false"
    # a non-bool falls back to the default rather than flipping semantics
    # (same convention as task_completion's require_no_error)
    fallback = _run(
        t, "contains", {"text": "paris", "case_sensitive": "no"}
    )
    assert fallback.score == 0.0
    assert fallback.details["case_sensitive"] == "true"


def test_contains_field_unobserved_is_none_not_fail() -> None:
    """No observed output → no verdict — absent evidence never fails."""
    t = _trace([_span("a1", "x")])
    res = _run(t, "contains", {"text": "Paris"})
    assert res.score is None
    assert res.details["reason"] == "field not observed as string"
    # `field` redirects the lookup — same helper as exact_match/token_f1
    t2 = _trace([_span("a1", "x", attrs={"answer": "it is Paris indeed"})])
    res2 = _run(t2, "contains", {"text": "Paris", "field": "answer"})
    assert res2.score == 1.0


def test_contains_malformed_params_evaluator_error() -> None:
    t = _output_trace()
    missing = _run(t, "contains", {})
    assert missing.score is None
    assert missing.metric == "evaluator_error"

    wrong_type = _run(t, "contains", {"text": 42})
    assert wrong_type.score is None
    assert wrong_type.metric == "evaluator_error"
    assert "string" in wrong_type.details["reason"]

    not_an_object = _run(t, "contains", "oops")
    assert not_an_object.score is None
    assert not_an_object.metric == "evaluator_error"


# -- regex --------------------------------------------------------------------


def test_regex_match_reports_span_and_substring() -> None:
    t = _output_trace()
    res = _run(t, "regex", {"pattern": r"capital of \w+"})
    assert res.score == 1.0
    assert res.metric == "output_regex_match"
    assert res.details["matched"] == "true"
    assert res.details["match"] == "capital of France"
    assert res.details["observed_in"] == "b1".ljust(16, "0")


def test_regex_no_match_and_case_insensitive() -> None:
    t = _output_trace()
    assert _run(t, "regex", {"pattern": "PARIS"}).score == 0.0
    insensitive = _run(
        t, "regex", {"pattern": "PARIS", "case_sensitive": False}
    )
    assert insensitive.score == 1.0


def test_regex_invalid_pattern_is_evaluator_error_not_crash() -> None:
    t = _output_trace()
    for bad in ("[", "(", "*", "\\", "a{2,1}"):
        res = _run(t, "regex", {"pattern": bad})
        assert res.score is None, bad
        assert res.metric == "evaluator_error"
        assert "invalid pattern" in res.details["reason"]


def test_regex_match_truncated_to_120_chars() -> None:
    t = _trace([_span("a1", "llm", attrs={"output.value": "x" * 500})])
    res = _run(t, "regex", {"pattern": "x+"})
    assert res.score == 1.0
    assert len(res.details["match"]) == 121  # 120 chars + ellipsis
    assert res.details["match"].endswith("…")


def test_regex_malformed_params_evaluator_error() -> None:
    t = _output_trace()
    assert _run(t, "regex", {}).metric == "evaluator_error"
    wrong_type = _run(t, "regex", {"pattern": 3.14})
    assert wrong_type.metric == "evaluator_error"
    # unobserved field → absent evidence is None, not a fail
    no_output = _run(_trace([_span("a1", "x")]), "regex", {"pattern": "y"})
    assert no_output.score is None
    assert no_output.details["reason"] == "field not observed as string"


# -- latency_budget ------------------------------------------------------------


def test_latency_budget_max_duration_ms_pass_and_fail() -> None:
    t = _trace([_span("a1", "work")])  # 10s envelope
    within = _run(t, "latency_budget", {"max_duration_ms": 20000})
    assert within.score == 1.0
    assert within.details["observed_ms"] == "10000.000"
    over = _run(t, "latency_budget", {"max_duration_ms": 5000})
    assert over.score == 0.0
    # the legacy `budget_ms` name stays accepted (CLI --budget-ms emits it)
    legacy = _run(t, "latency_budget", {"budget_ms": 20000})
    assert legacy.score == 1.0


def test_latency_budget_unended_trace_is_none() -> None:
    """An in-flight trace has no duration — None, never a fabricated fail."""
    t = _trace([_span("a1", "work")], ended=False)
    res = _run(t, "latency_budget", {"max_duration_ms": 20000})
    assert res.score is None
    assert res.details["reason"] == "unended trace — duration unknown"


def test_latency_budget_bad_params_evaluator_error() -> None:
    t = _trace([_span("a1", "work")])
    bad_params: list[dict[str, Any]] = [
        {},                                   # missing bound
        {"max_duration_ms": "5000"},          # wire string, not a number
        {"max_duration_ms": True},            # bool must not become 1.0
        {"max_duration_ms": float("inf")},    # non-finite
        {"max_duration_ms": float("nan")},    # non-finite
        {"max_duration_ms": 0},               # must be positive
        {"max_duration_ms": -100},            # must be positive
        {"max_duration_ms": None},            # non-numeric
    ]
    for params in bad_params:
        res = _run(t, "latency_budget", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params


# -- tool_call_count -----------------------------------------------------------


def _tools_trace() -> Trace:
    return _trace(
        [
            _span("a1", "agent", kind=SpanKind.AGENT),
            _span("c1", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"}),
            _span("c2", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"}),
            _span("c3", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "db_query"}),
            _span("c4", "call", kind=SpanKind.TOOL),  # no tool.name attr
            _span("d1", "llm", kind=SpanKind.LLM),    # not a tool span
        ]
    )


def test_tool_call_count_all_tool_spans() -> None:
    t = _tools_trace()
    within = _run(t, "tool_call_count", {"min": 1, "max": 4})
    assert within.score == 1.0
    assert within.metric == "tool_calls_within_bounds"
    assert within.details["count"] == "4"  # TOOL kind only, attr or not
    assert within.details["tool"] == "*"
    # inclusive bounds — exactly at the edge still passes
    edge = _run(t, "tool_call_count", {"min": 4, "max": 4})
    assert edge.score == 1.0
    # out of bounds both directions
    assert _run(t, "tool_call_count", {"min": 5, "max": 9}).score == 0.0
    assert _run(t, "tool_call_count", {"min": 0, "max": 3}).score == 0.0


def test_tool_call_count_tool_filter() -> None:
    t = _tools_trace()
    res = _run(t, "tool_call_count",
               {"min": 2, "max": 2, "tool": "web_search"})
    assert res.score == 1.0
    assert res.details["count"] == "2"
    assert res.details["tool"] == "web_search"
    # only the attribute counts — a TOOL span without `tool.name` is not
    # attributed to the filter (evidence, never a guessed name)
    one = _run(t, "tool_call_count", {"min": 1, "max": 1, "tool": "db_query"})
    assert one.score == 1.0
    # an unobserved tool honestly counts zero — and can still fail bounds
    zero = _run(t, "tool_call_count", {"min": 1, "max": 5, "tool": "nope"})
    assert zero.score == 0.0
    assert zero.details["count"] == "0"


def test_tool_call_count_bad_params_evaluator_error() -> None:
    t = _tools_trace()
    bad_params: list[dict[str, Any]] = [
        {"max": 2},                       # missing min
        {"min": 0},                       # missing max
        {"min": 3, "max": 1},             # inverted bounds
        {"min": -1, "max": 2},            # negative bound
        {"min": "1", "max": 2},           # wire string, not an int
        {"min": True, "max": 2},          # bool is not a count
        {"min": 0.5, "max": 2},           # fractional float
        {"min": 0, "max": float("nan")},  # non-finite
        {"min": 0, "max": 2, "tool": 7},  # tool must be a string
    ]
    for params in bad_params:
        res = _run(t, "tool_call_count", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params
