"""Cycle-13.1 audit — evaluator coverage gaps and stats cross-checks.

Two axes, both pinned against independently computed values (no scipy
in the dev venv — every expected number below is hand-verified in this
file's docstring / computed from first principles, never copied from
the implementation under test):

* evaluators — every registered evaluator is exercised for params,
  defaults, bound violations (``evaluator_error``, never a raised
  exception), honest ``score=None`` on missing evidence, and
  determinism (identical input → byte-identical result dumps).
* compare stats — Wilson CI / two-proportion z / Mann-Whitney U
  reference values recomputed with definition-level formulas, plus the
  regression-gate rule and the insufficient-data floor.

Reference values used (hand-computed, z=1.96 exactly — the module's
``_Z_CRIT``):

* Wilson k=5,  n=20  → [0.111860, 0.468705]
* Wilson k=1,  n=3   → [0.061490, 0.792345]
* Wilson k=450,n=1000→ [0.419415, 0.480968]
* z-test 90/100 vs 80/100 → z≈1.9803, p≈0.047670 (borderline
  significant — exercises both sides of the 0.05 boundary)
* MWU [1,2,5] vs [3,4,6] (no ties) → U=2.0, p≈0.382733
* MWU [0.5,0.5,1.0] vs [0.5,1.0,2.0] (tied groups of 3 and 2) →
  U=2.5, tie-corrected var=4.5, p≈0.479500
* MWU interleaved pairs [1,1,2,2,3,3,4,4,5,5] vs
  [1.5,1.5,2.5,2.5,3.5,3.5,4.5,4.5,5.5,5.5] → U=40.0, p≈0.471003
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_lens.contracts.v1.evaluation import EvaluationResult
from forge_lens.contracts.v1.trace import Span, SpanKind, Trace
from forge_lens.eval import EVALUATORS, evaluate_traces, run_evaluators
from forge_lens.eval.compare import _mann_whitney, _wilson_ci

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TRACE_ID = "b" * 32


def _span(
    sid: str,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    dur_ms: float = 100.0,
    attrs: dict[str, Any] | None = None,
    error: bool = False,
) -> Span:
    from forge_lens.contracts.v1.trace import Status, StatusCode

    s = Span(
        trace_id=TRACE_ID,
        span_id=sid.ljust(16, "0")[:16],
        name=name,
        kind=kind,
        start_time=T0,
        end_time=T0 + timedelta(milliseconds=dur_ms),
        attributes=attrs or {},
    )
    if error:
        s.status = Status(code=StatusCode.ERROR, message="boom")
    return s


def _trace(
    spans: list[Span],
    *,
    ended: bool = True,
    trace_id: str = TRACE_ID,
    resource_attrs: dict[str, Any] | None = None,
) -> Trace:
    return Trace(
        trace_id=trace_id,
        project_id="p1",
        started_at=T0,
        ended_at=T0 + timedelta(seconds=10) if ended else None,
        spans=spans,
        resource_attributes=resource_attrs or {},
    )


def _run(
    trace: Trace, name: str, params: Any = None
) -> EvaluationResult:
    return run_evaluators(trace, [{"name": name, "params": params}])[0]


def _usage(input_tokens: int, output_tokens: int) -> dict[str, Any]:
    return {
        "forge_lens.token_usage": {
            "model": "gpt-test",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
    }


# -- registry-wide invariants ---------------------------------------------------


def test_registry_is_complete_and_sorted() -> None:
    """The registry is the documented 17; iteration order is sorted so
    any consumer iterating it emits deterministic output."""
    assert len(EVALUATORS) == 17
    assert sorted(EVALUATORS) == [
        "allowed_tools",
        "claims_grounded",
        "contains",
        "error_free",
        "evidence_coverage",
        "exact_match",
        "has_span",
        "human_review",
        "latency_budget",
        "precision_recall",
        "regex",
        "schema_validation",
        "task_completion",
        "token_budget",
        "token_f1",
        "tool_call_count",
        "tool_success",
    ]


def test_every_evaluator_empty_trace_and_params_is_honest() -> None:
    """No registered evaluator may *raise* through run_evaluators or
    fabricate a pass on an empty trace with empty params — the only
    legal outcomes are a scored verdict on real evidence, ``None`` with
    a reason, or ``evaluator_error``."""
    t = _trace([])
    for name in sorted(EVALUATORS):
        res = _run(t, name)
        assert res.trace_reference == TRACE_ID
        if res.score is None:
            assert res.details.get("reason"), name
        else:
            # a scored result on an empty trace is only honest for
            # evaluators whose evidence is trace-level (e.g. error_free
            # on zero error spans, evidence-empty counts)
            assert res.score in (0.0, 1.0) or not EVALUATORS[name][1], name


def test_every_evaluator_deterministic_identical_runs() -> None:
    """Same input twice → identical dumps (no clock, no dict-order
    drift): the engine contract is byte-determinism."""
    t = _trace(
        [
            _span("a1", "agent", kind=SpanKind.AGENT),
            _span("b1", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"}),
            _span("c1", "llm", kind=SpanKind.LLM,
                  attrs={"output.value": "alpha beta", **_usage(5, 7)}),
        ]
    )
    specs: list[dict[str, Any]] = [
        {"name": "error_free"},
        {"name": "tool_success"},
        {"name": "has_span", "params": {"kind": "tool"}},
        {"name": "task_completion"},
        {"name": "token_budget", "params": {"max_total_tokens": 100}},
        {"name": "latency_budget", "params": {"max_duration_ms": 20000}},
        {"name": "token_f1",
         "params": {"field": "output.value", "expected": "alpha beta"}},
        {"name": "allowed_tools", "params": {"allowed": ["web_search"]}},
        {"name": "contains", "params": {"text": "alpha"}},
        {"name": "regex", "params": {"pattern": r"alpha"}},
        {"name": "tool_call_count", "params": {"min": 0, "max": 5}},
        {"name": "exact_match",
         "params": {"field": "output.value", "expected": "alpha beta"}},
        {"name": "schema_validation",
         "params": {"schema": {"type": "string"}}},
        {"name": "evidence_coverage",
         "params": {"required_attrs": ["tool.name", "output.value"]}},
        {"name": "human_review"},
        {"name": "precision_recall",
         "params": {"tool": True, "expected_items": ["web_search"]}},
        {"name": "claims_grounded",
         "params": {"required_source_attr": "tool.name"}},
    ]
    first = [r.model_dump(mode="json") for r in run_evaluators(t, specs)]
    second = [r.model_dump(mode="json") for r in run_evaluators(t, specs)]
    assert first == second
    names: list[Any] = [s["name"] for s in specs]
    assert [r["evaluator"] for r in first] == names


def test_spec_and_params_shape_errors_never_raise() -> None:
    """run_evaluators degrades malformed spec entries to scored-None
    results — a typo'd battery is reported, not crashed."""
    t = _trace([_span("a1", "x")])
    results = run_evaluators(
        t,
        [
            "not-a-dict",                          # spec not an object
            {"name": "error_free", "params": "x"},  # params not object
            {"name": "does_not_exist"},
            {},                                    # nameless spec
        ],
    )
    assert [r.metric for r in results] == [
        "evaluator_error",
        "evaluator_error",
        "unknown_evaluator",
        "unknown_evaluator",
    ]
    assert all(r.score is None for r in results)
    assert results[0].evaluator == "invalid_spec"
    assert results[3].evaluator == "unknown"


# -- error_free ----------------------------------------------------------------


def test_error_free_multiple_errors_named_and_deterministic() -> None:
    t = _trace(
        [
            _span("a1", "x", error=True),
            _span("b2", "y"),
            _span("c3", "z", error=True),
        ]
    )
    res = _run(t, "error_free")
    assert res.score == 0.0
    assert res.details["error_count"] == "2"
    assert res.details["error_span_ids"] == ",".join(
        [s.ljust(16, "0") for s in ("a1", "c3")]
    )


def test_error_free_empty_trace_is_pass_not_null() -> None:
    """Zero spans → zero error spans → 1.0: 'no error observed' is real
    evidence for this metric, not a null."""
    assert _run(_trace([]), "error_free").score == 1.0


# -- latency_budget -------------------------------------------------------------


def test_latency_budget_empty_and_negative_envelope_are_null() -> None:
    """Empty trace has no duration evidence; a negative envelope (clock
    skew) is unverifiable — both are ``None``, never a fail."""
    empty = _run(_trace([]), "latency_budget", {"max_duration_ms": 100})
    assert empty.score is None
    assert "empty trace" in empty.details["reason"]

    backwards = Trace(
        trace_id=TRACE_ID,
        project_id="p1",
        started_at=T0 + timedelta(seconds=10),
        ended_at=T0,
        spans=[_span("a1", "x")],
    )
    neg = _run(backwards, "latency_budget", {"max_duration_ms": 100})
    assert neg.score is None
    assert "negative" in neg.details["reason"]


def test_latency_budget_exact_boundary_passes() -> None:
    t = _trace([_span("a1", "work")])  # 10s envelope
    assert _run(t, "latency_budget", {"max_duration_ms": 10000}).score == 1.0
    assert _run(t, "latency_budget", {"max_duration_ms": 9999.5}).score == 0.0


# -- token_budget ----------------------------------------------------------------


def test_token_budget_bound_violations_are_evaluator_error() -> None:
    """Negative or fractional budgets are malformed specs — reporting a
    score=0.0 would masquerade a spec bug as a trace verdict."""
    t = _trace([_span("a1", "llm", attrs=_usage(3, 2), kind=SpanKind.LLM)])
    for bad in (-1, -0.5, 100.5, float("inf"), float("nan"), None,
                [100], {"b": 1}, True, "500"):
        res = _run(t, "token_budget", {"max_total_tokens": bad})
        assert res.score is None, bad
        assert res.metric == "evaluator_error", bad


def test_token_budget_zero_budget_and_unobserved_spans() -> None:
    """budget=0 is legal ('no tokens allowed'); spans without usage are
    evidence flagged as a caveat, never counted as zero-cost."""
    no_usage = _trace([_span("a1", "x")])
    res = _run(no_usage, "token_budget", {"max_total_tokens": 0})
    assert res.score == 1.0  # zero observed tokens ≤ 0
    assert res.details["spans_without_usage"] == "1"
    assert "unobserved" in res.details["caveat"]

    mixed = _trace([
        _span("a1", "llm", kind=SpanKind.LLM, attrs=_usage(10, 10)),
        _span("b1", "llm", kind=SpanKind.LLM),  # no usage block
    ])
    res2 = _run(mixed, "token_budget", {"max_total_tokens": 20})
    assert res2.score == 1.0
    assert res2.details["total_tokens"] == "20"
    assert res2.details["spans_with_usage"] == "1"
    assert res2.details["spans_without_usage"] == "1"


def test_token_budget_missing_param_is_evaluator_error() -> None:
    res = _run(_trace([_span("a1", "x")]), "token_budget")
    assert res.score is None
    assert res.metric == "evaluator_error"


# -- tool_success ----------------------------------------------------------------


def test_tool_success_rate_math_and_null_on_no_tools() -> None:
    t = _trace(
        [
            _span("a1", "call", kind=SpanKind.TOOL),
            _span("b2", "call", kind=SpanKind.TOOL, error=True),
            _span("c3", "call", kind=SpanKind.TOOL),
            _span("d4", "llm", kind=SpanKind.LLM, error=True),
        ]
    )
    res = _run(t, "tool_success")
    # 2 of 3 tool spans ok; the errored LLM span is not a tool call —
    # and the score is a *rate* (0.667), while pass_rate counts only
    # score==1.0 traces (binary flag convention documented in EVALUATORS)
    assert res.score == pytest.approx(0.666667, abs=1e-6)
    assert res.details["tool_spans"] == "3"
    assert res.details["failed"] == "b2".ljust(16, "0")

    res_none = _run(_trace([_span("a1", "x")]), "tool_success")
    assert res_none.score is None
    assert "no tool spans" in res_none.details["reason"]


# -- has_span -------------------------------------------------------------------


def test_has_span_name_kind_and_neutral_filters() -> None:
    t = _trace(
        [
            _span("a1", "agent-loop", kind=SpanKind.AGENT),
            _span("b2", "search", kind=SpanKind.TOOL),
        ]
    )
    by_name = _run(t, "has_span", {"name": "search"})
    assert by_name.score == 1.0
    assert by_name.details["matched"] == "b2".ljust(16, "0")
    assert _run(t, "has_span", {"name": "missing"}).score == 0.0
    by_kind = _run(t, "has_span", {"kind": "tool"})
    assert by_kind.score == 1.0
    both = _run(t, "has_span", {"name": "search", "kind": "tool"})
    assert both.score == 1.0
    assert _run(t, "has_span", {"name": "search", "kind": "llm"}).score == 0.0
    # no filters at all → "any span at all" — honest existential query
    assert _run(t, "has_span").score == 1.0
    assert _run(_trace([]), "has_span").score == 0.0


def test_has_span_malformed_filters_are_evaluator_error() -> None:
    """A non-string name or a kind outside the closed enum can never
    match honestly — they are spec bugs, not silent misses."""
    t = _trace([_span("a1", "x")])
    for params in ({"name": 42}, {"name": ["x"]}, {"kind": 7},
                   {"kind": "tol"}, {"kind": "TOOL"}, {"kind": ""}):
        res = _run(t, "has_span", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params


# -- exact_match ------------------------------------------------------------------


def test_exact_match_expected_types_and_mismatch() -> None:
    t = _trace([_span("a1", "x", attrs={"out": {"k": 1}})])
    hit = _run(t, "exact_match", {"field": "out", "expected": {"k": 1}})
    assert hit.score == 1.0
    assert hit.details["observed_in"] == "a1".ljust(16, "0")
    miss = _run(t, "exact_match", {"field": "out", "expected": {"k": 2}})
    assert miss.score == 0.0
    assert miss.details["observed"] == repr({"k": 1})


def test_exact_match_null_observation_is_real_evidence() -> None:
    """``field`` present with a JSON null value IS observed — it must
    compare as null, not collapse into 'not observed'."""
    t = _trace([_span("a1", "x")], resource_attrs={"answer": None})
    res = _run(t, "exact_match", {"field": "answer", "expected": None})
    assert res.score == 1.0
    assert res.details["observed_in"] == "resource_attributes"
    miss = _run(t, "exact_match", {"field": "answer", "expected": "x"})
    assert miss.score == 0.0  # observed null ≠ 'x' — mismatch, not absent
    # a span-level null observes the same way
    t2 = _trace([_span("a1", "x", attrs={"answer": None})])
    res2 = _run(t2, "exact_match", {"field": "answer", "expected": None})
    assert res2.score == 1.0
    assert res2.details["observed_in"] == "a1".ljust(16, "0")


def test_exact_match_field_param_validation() -> None:
    t = _trace([_span("a1", "x", attrs={"f": "v"})])
    for params in ({}, {"field": ""}, {"field": 7}, {"field": None}):
        res = _run(t, "exact_match", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params


# -- token_f1 (evaluator-level; compare coverage lives in test_eval_compare) -----


def test_token_f1_score_math_and_params() -> None:
    t = _trace([_span("a1", "llm", attrs={"output.value": "alpha beta gamma"})])
    res = _run(t, "token_f1",
               {"field": "output.value", "expected": "alpha beta delta"})
    # common={alpha,beta}=2 → p=2/3, r=2/3 → f1=0.666667
    assert res.score == pytest.approx(0.666667, abs=1e-6)
    assert res.details["precision"].startswith("0.666")
    assert res.details["recall"].startswith("0.666")


def test_token_f1_empty_and_non_string_evidence() -> None:
    """Both tokenless → 1.0 ('both empty' is declared evidence); a
    non-string observation is unobservable → ``None``."""
    punct = _trace([_span("a1", "x", attrs={"out": "!!!"})])
    res = _run(punct, "token_f1", {"field": "out", "expected": "???"})
    assert res.score == 1.0
    assert res.details["reason"] == "both empty"

    non_str = _trace([_span("a1", "x", attrs={"out": ["a", "b"]})])
    res2 = _run(non_str, "token_f1", {"field": "out", "expected": "a"})
    assert res2.score is None
    assert "not observed as string" in res2.details["reason"]

    for params in ({"expected": "x"},            # field missing
                   {"field": "out"},             # expected missing
                   {"field": "out", "expected": 7},
                   {"field": "out", "expected": None}):
        res3 = _run(punct, "token_f1", params)
        assert res3.score is None, params
        assert res3.metric == "evaluator_error", params


# -- allowed_tools ---------------------------------------------------------------


def test_allowed_tools_semantic_names_and_violations() -> None:
    t = _trace(
        [
            _span("a1", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"}),
            _span("b2", "raw_http", kind=SpanKind.TOOL),  # no tool.name
        ]
    )
    ok = _run(t, "allowed_tools",
              {"allowed": ["web_search", "raw_http"]})
    assert ok.score == 1.0
    bad = _run(t, "allowed_tools", {"allowed": ["web_search"]})
    assert bad.score == 0.0
    assert bad.details["violations"] == "raw_http"
    # violations are a sorted set — one name, many calls
    t2 = _trace(
        [
            _span(f"{i:x}", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "db"})
            for i in range(3)
        ]
    )
    res2 = _run(t2, "allowed_tools", {"allowed": []})
    assert res2.score == 0.0
    assert res2.details["violations"] == "db"


def test_allowed_tools_empty_trace_and_params_validation() -> None:
    """No TOOL spans → vacuous pass (empty set ⊆ allowlist); malformed
    ``allowed`` is a spec bug."""
    res = _run(_trace([_span("a1", "x")]), "allowed_tools",
               {"allowed": []})
    assert res.score == 1.0
    assert res.details["tool_spans"] == "0"

    t = _trace([_span("a1", "call", kind=SpanKind.TOOL)])
    for params in ({}, {"allowed": "web_search"}, {"allowed": [1, 2]},
                   {"allowed": None}, {"allowed": ["a", 3]}):
        res = _run(t, "allowed_tools", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params


# -- task_completion -------------------------------------------------------------


def test_task_completion_in_flight_is_fail_not_null() -> None:
    """An unended trace is *measurably* not complete — 0.0, unlike
    latency_budget's unknown-duration ``None``."""
    t = _trace([_span("a1", "x")], ended=False)
    res = _run(t, "task_completion")
    assert res.score == 0.0
    assert "no end time" in res.details["reason"]


def test_task_completion_require_no_error_semantics() -> None:
    t = _trace([_span("a1", "x", error=True)])
    assert _run(t, "task_completion").score == 0.0  # default strict
    relaxed = _run(t, "task_completion", {"require_no_error": False})
    assert relaxed.score == 1.0
    assert relaxed.details["require_no_error"] == "false"
    # a non-bool falls back to the strict default, never inverts
    fallback = _run(t, "task_completion", {"require_no_error": "no"})
    assert fallback.score == 0.0
    assert fallback.details["require_no_error"] == "true"


# -- schema_validation: deep-input crash guard -----------------------------------


def test_schema_validation_extreme_depth_string_never_crashes() -> None:
    """A string attribute nested past the interpreter's JSON parse depth
    must degrade to a verdict — RecursionError escapes are crashes, and
    the runner deliberately does not blanket-catch them."""
    deep = "[" * 100_000 + "]" * 100_000  # leaf attr → past ingest cap
    t = _trace([_span("a1", "llm", attrs={"output.value": deep})])
    res = _run(t, "schema_validation", {"schema": {}})
    assert res.score is not None  # parsed or string — never raises
    res2 = _run(t, "schema_validation",
                {"schema": {"type": "object"}})
    assert res2.score is not None


def test_precision_recall_extreme_depth_string_never_crashes() -> None:
    """Same ``json.loads`` path in ``_extract_items`` — unparseable-by-
    depth is the singleton fallback, never an escaping exception."""
    deep = "[" * 100_000 + "1" + "]" * 100_000
    t = _trace([_span("a1", "x", attrs={"ents": deep})])
    res = _run(t, "precision_recall",
               {"field": "ents", "expected_items": ["1"]})
    assert res.score is not None


def test_schema_validation_null_resource_attr_is_observed() -> None:
    """The null-value bug fix: a resource attr observed as JSON null
    validates against ``{\"type\": \"null\"}`` and fails ``object`` —
    presence ≠ non-nullity."""
    t = _trace([_span("a1", "x")], resource_attrs={"payload": None})
    res = _run(t, "schema_validation",
               {"field": "payload", "schema": {"type": "null"}})
    assert res.score == 1.0
    assert res.details["instance_kind"] == "null"
    bad = _run(t, "schema_validation",
               {"field": "payload", "schema": {"type": "object"}})
    assert bad.score == 0.0
    assert "expected object, got null" in bad.details["violations"]


# -- stats cross-checks (definition-level reference values) ----------------------


def test_wilson_ci_reference_values() -> None:
    """Hand-computed Wilson bounds (z=1.96) — never derived by copying
    the implementation's formula."""
    assert _wilson_ci(0.25, 20) == pytest.approx([0.111860, 0.468705],
                                                abs=1e-6)
    assert _wilson_ci(1 / 3, 3) == pytest.approx([0.061490, 0.792345],
                                               abs=1e-6)
    assert _wilson_ci(0.45, 1000) == pytest.approx([0.419415, 0.480968],
                                                 abs=1e-6)


def test_mann_whitney_reference_values_with_and_without_ties() -> None:
    """U from pairwise counting + Siegel&Castellan tie correction —
    recomputed independently of the rank-sum implementation."""
    res = _mann_whitney([1.0, 2.0, 5.0], [3.0, 4.0, 6.0])
    assert res is not None
    u, p = res
    assert u == 2.0
    assert p == pytest.approx(0.382733, abs=1e-6)

    res = _mann_whitney([0.5, 0.5, 1.0], [0.5, 1.0, 2.0])
    assert res is not None
    u, p = res
    assert u == 2.5                       # ties: 0.5 x3 group, 1.0 x2 group
    assert p == pytest.approx(0.479500, abs=1e-6)

    res = _mann_whitney(
        [1.0, 1.0, 2.0, 2.0, 3.0, 3.0, 4.0, 4.0, 5.0, 5.0],
        [1.5, 1.5, 2.5, 2.5, 3.5, 3.5, 4.5, 4.5, 5.5, 5.5],
    )
    assert res is not None
    u, p = res
    assert u == 40.0
    assert p == pytest.approx(0.471003, abs=1e-6)


def test_mann_whitney_antisymmetry_and_min_u() -> None:
    """U(A,B) + U(B,A) = n1·n2 — the two-sided p is identical either way."""
    xs = [0.4, 0.666667, 1.0, 0.5, 0.0] * 3
    ys = [0.9, 0.7, 0.8, 0.6, 1.0] * 3
    fwd = _mann_whitney(xs, ys)
    rev = _mann_whitney(ys, xs)
    assert fwd is not None
    assert rev is not None
    assert fwd[0] + rev[0] == pytest.approx(len(xs) * len(ys))
    assert fwd[1] == pytest.approx(rev[1])


# -- summary/runner honesty ------------------------------------------------------


def test_summary_counts_nulls_and_scores_separately() -> None:
    """n counts scored results only; nulls are their own column and
    pass_rate is passes/n — a fabricated zero would poison both."""
    traces = [
        _trace([_span("a1", "call", kind=SpanKind.TOOL)],
               trace_id="a" * 32),
        _trace([_span("a1", "x")], trace_id="b" * 32),  # no tool span
    ]
    report = evaluate_traces(traces, [{"name": "tool_success"}])
    slot = report.summary["tool_success/tool_success_rate"]
    assert slot["n"] == 1
    assert slot["nulls"] == 1
    assert slot["pass_rate"] == 1.0
    assert slot["mean_score"] == 1.0


def test_duplicate_trace_ids_evaluate_once() -> None:
    t = _trace([_span("a1", "x")])
    report = evaluate_traces([t, t, t], [{"name": "error_free"}])
    assert report.trace_ids == [TRACE_ID]
    assert report.summary["error_free/no_error_spans"]["n"] == 1


def test_evaluated_at_is_none_without_injected_clock() -> None:
    """Determinism: no wall clock is read — evaluated_at stays absent
    unless the caller injects ``now``."""
    t = _trace([_span("a1", "x")])
    res = _run(t, "error_free")
    assert res.evaluated_at is None
    stamped = run_evaluators(t, [{"name": "error_free"}], now=T0)[0]
    assert stamped.evaluated_at == T0


# -- scripts/eval_gate.py --------------------------------------------------------
#
# The CI gate script is loaded in-process (no subprocess) and driven
# through main(argv) — its stdout JSON and exit codes are the contract
# this audit pins.


def _load_gate_module() -> Any:
    import importlib.util

    path = (
        Path(__file__).resolve().parents[2] / "scripts" / "eval_gate.py"
    )
    spec = importlib.util.spec_from_file_location("eval_gate", path)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _seed_gate_db(path: Path) -> tuple[list[str], list[str]]:
    """12 clean baseline traces + 12 candidate traces (4 with a failing
    tool call). Returns (baseline_ids, candidate_ids)."""
    from forge_lens.storage.sqlite import SQLiteTraceStore

    store = SQLiteTraceStore(path)

    def mk(tid: str, fail: bool) -> Trace:
        spans = [
            _span("11", "agent", kind=SpanKind.AGENT),
            _span("22", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"},
                  error=fail),
        ]
        for s in spans:
            s.trace_id = tid
        return Trace(
            trace_id=tid, project_id="default", started_at=T0,
            ended_at=T0 + timedelta(seconds=2), spans=spans,
        )

    base = [f"{0xAAAA000000000000000000000000 + i:032x}" for i in range(12)]
    cand = [f"{0xBBBB000000000000000000000000 + i:032x}" for i in range(12)]
    for tid in base:
        store.upsert_trace(mk(tid, fail=False))
    for i, tid in enumerate(cand):
        store.upsert_trace(mk(tid, fail=i >= 8))
    store.close()
    return base, cand


def _gate_args(tmp_path: Path, base: list[str], cand: list[str],
               gate: str | None = None) -> list[str]:
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({
        "dataset_id": "qa-suite",
        "evaluators": [{"name": "error_free"}, {"name": "tool_success"}],
    }))
    bf = tmp_path / "base.json"
    bf.write_text(json.dumps({"trace_ids": base}))
    argv = [
        "--db", str(tmp_path / "lens.db"),
        "--dataset", str(suite),
        "--baseline", f"@{bf}",
        "--candidate", json.dumps({"trace_ids": cand}),
    ]
    if gate is not None:
        argv += ["--gate", gate]
    return argv


def test_gate_regression_exit_1(tmp_path: Path,
                                capsys: pytest.CaptureFixture[str]) -> None:
    gate = _load_gate_module()
    base, cand = _seed_gate_db(tmp_path / "lens.db")
    code = gate.main(
        _gate_args(tmp_path, base, cand,
                   gate='{"fail_on_significant_regression": true}')
    )
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"]["status"] == "regression"
    assert out["verdict"]["evidence_failures"]
    assert out["gate"]["gate_passed"] is False


def test_gate_pass_exit_0_and_deterministic(tmp_path: Path,
                                            capsys: pytest.CaptureFixture[str]
                                            ) -> None:
    """Identical inputs → identical report bytes: the JSON document is
    the machine contract and must be byte-stable."""
    gate = _load_gate_module()
    base, _cand = _seed_gate_db(tmp_path / "lens.db")
    args = _gate_args(
        tmp_path, base, base,
        gate='{"metrics": {"error_free/no_error_spans": '
             '{"min_pass_rate_b": 1.0}}}',
    )
    assert gate.main(args) == 0
    first = capsys.readouterr().out
    assert gate.main(args) == 0
    second = capsys.readouterr().out
    assert first == second
    report = json.loads(first)
    assert report["verdict"]["status"] == "pass"
    assert "evaluated_at" not in first


def test_gate_thin_data_never_fails(tmp_path: Path) -> None:
    """n=8/side is below the significance floor; identical clean cohorts
    with an untestable metric → insufficient_data, never regression and
    never a vacuous pass."""
    gate = _load_gate_module()
    base, _cand = _seed_gate_db(tmp_path / "lens.db")
    code = gate.main(
        _gate_args(tmp_path, base[:8], base[4:],
                   gate='{"fail_on_significant_regression": true}')
    )
    assert code == 2
    code2 = gate.main(
        _gate_args(tmp_path, base, base,
                   gate='{"metrics": {"unmeasured/metric": '
                        '{"min_pass_rate_b": 1.0}}}')
    )
    assert code2 == 2  # gating on unmeasured evidence is insufficient


def test_gate_missing_traces_is_insufficient(tmp_path: Path) -> None:
    gate = _load_gate_module()
    base, cand = _seed_gate_db(tmp_path / "lens.db")
    code = gate.main(
        _gate_args(tmp_path, [*base, "f" * 32], cand)
    )
    assert code == 2


def test_gate_usage_errors_exit_64(tmp_path: Path) -> None:
    gate = _load_gate_module()
    base, cand = _seed_gate_db(tmp_path / "lens.db")
    # empty gate spec — asserts nothing → misconfiguration, not a pass
    with pytest.raises(SystemExit) as exc:
        gate.main(_gate_args(tmp_path, base, cand, gate="{}"))
    assert exc.value.code == 64
    # missing dataset record in the store
    with pytest.raises(SystemExit) as exc2:
        gate.main([
            "--db", str(tmp_path / "lens.db"),
            "--dataset", "no-such-dataset",
            "--baseline", json.dumps({"trace_ids": base}),
            "--candidate", json.dumps({"trace_ids": cand}),
        ])
    assert exc2.value.code == 64
    # argparse usage error remapped off 2 (2 = insufficient data)
    with pytest.raises(SystemExit) as exc3:
        gate.main(["--dataset"])
    assert exc3.value.code == 64


def test_apply_gate_max_drop_null_pass_rate_fails_as_unmeasured(
) -> None:
    """max_drop on a metric never observed on one side is a
    missing-evidence failure — never a vacuous pass."""
    from forge_lens.eval.compare import apply_gate

    result = {"deltas": [{
        "evaluator": "token_f1", "metric": "score",
        "pass_rate_a": 0.9, "pass_rate_b": None,
        "n_a": 12, "n_b": 0,
    }]}
    out = apply_gate(
        result, {"metrics": {"token_f1/score": {"max_drop": 0.10}}})
    assert out["gate_passed"] is False
    (f,) = out["failures"]
    assert f["observed"] is None
    assert "unmeasured" in f["reason"]


def test_gate_max_drop_unmeasured_is_insufficient_e2e(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    """Candidate cohort with no tool spans → tool_success yields
    pass_rate_b=None → max_drop cannot be evaluated → exit 2, not 0."""
    from forge_lens.storage.sqlite import SQLiteTraceStore

    gate = _load_gate_module()
    store = SQLiteTraceStore(tmp_path / "lens.db")

    def mk(tid: str, with_tool: bool) -> Trace:
        spans = [_span("11", "agent", kind=SpanKind.AGENT)]
        if with_tool:
            spans.append(_span("22", "call", kind=SpanKind.TOOL,
                               attrs={"tool.name": "web_search"}))
        for s in spans:
            s.trace_id = tid
        return Trace(trace_id=tid, project_id="default", started_at=T0,
                     ended_at=T0 + timedelta(seconds=2), spans=spans)

    base = [f"{0xCCCC000000000000000000000000 + i:032x}" for i in range(4)]
    cand = [f"{0xDDDD000000000000000000000000 + i:032x}" for i in range(4)]
    for tid in base:
        store.upsert_trace(mk(tid, with_tool=True))
    for tid in cand:
        store.upsert_trace(mk(tid, with_tool=False))
    store.close()

    code = gate.main(_gate_args(
        tmp_path, base, cand,
        gate='{"metrics": {"tool_success/tool_success_rate": '
             '{"max_drop": 0.05}}}',
    ))
    assert code == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"]["status"] == "insufficient_data"
    assert out["verdict"]["missing_evidence_failures"]


def test_gate_bad_config_exits_64(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed FORGE_LENS_* config is a usage error — a traceback
    exit-1 would be misread by CI as a regression."""
    monkeypatch.setenv("FORGE_LENS_AUTH_MODE", "not-a-mode")
    gate = _load_gate_module()
    with pytest.raises(SystemExit) as exc:
        gate.main([
            "--dataset", "x",
            "--baseline", json.dumps({"trace_ids": []}),
            "--candidate", json.dumps({"trace_ids": []}),
        ])
    assert exc.value.code == 64


def test_gate_reason_strings_match_compare_contract() -> None:
    """eval_gate.py classifies apply_gate failures by their ``reason``
    strings — this pins the exact wording so a drift in compare.py is
    a loud test break, not a silent misclassification."""
    src = (
        Path(__file__).resolve().parents[2]
        / "src" / "forge_lens" / "eval" / "compare.py"
    ).read_text(encoding="utf-8")
    for reason in (
        "metric not evaluated — cannot gate on unmeasured evidence",
        "pass_rate_b below minimum",
        "pass_rate dropped more than allowed",
        "max_drop unmeasured — pass rate missing on one or both sides",
        "statistically significant regression",
        "metric gate spec must be an object",
        "gate.metrics must be an object",
    ):
        assert reason in src, reason
