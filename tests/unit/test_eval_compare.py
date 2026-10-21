"""Unit tests for eval/compare statistical machinery — Wilson score
intervals on binary pass rates, Mann-Whitney U on continuous score
distributions, the multiple-comparisons note, and gate semantics.

Every case uses known distributions: claims are asserted against
hand-checkable statistics, never against values the code happens to
emit.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.eval import compare_runs, evaluate_traces
from forge_lens.eval.compare import _mann_whitney, _wilson_ci, apply_gate
from forge_lens.eval.runner import EvalReport

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
EXPECTED = "alpha beta gamma delta"  # 4 distinct tokens
F1_SPEC: list[dict[str, Any]] = [
    {"name": "token_f1",
     "params": {"field": "output.value", "expected": EXPECTED}}
]
BINARY_SPEC: list[dict[str, Any]] = [{"name": "error_free"}]


def _span(sid: str, name: str, *, attrs: dict[str, Any] | None = None,
          error: bool = False) -> Span:
    s = Span(
        trace_id="0" * 32,
        span_id=sid.ljust(16, "0")[:16],
        name=name,
        kind=SpanKind.INTERNAL,
        start_time=T0,
        end_time=T0 + timedelta(milliseconds=100),
        attributes=attrs or {},
    )
    if error:
        s.status = Status(code=StatusCode.ERROR, message="boom")
    return s


def _trace(tid: str, spans: list[Span]) -> Trace:
    return Trace(
        trace_id=tid,
        project_id="p1",
        started_at=T0,
        ended_at=T0 + timedelta(seconds=10),
        spans=spans,
    )


def _binary_report(
    passes: int, n: int, dataset: str, id_offset: int
) -> EvalReport:
    """``passes`` of ``n`` error-free traces → pass_rate = passes/n."""
    traces = [
        _trace(f"{id_offset + i:032x}", [_span("a1", "op", error=i >= passes)])
        for i in range(n)
    ]
    return evaluate_traces(traces, BINARY_SPEC, dataset=dataset)


def _f1_report(observed: list[str], dataset: str, id_offset: int) -> EvalReport:
    """One token_f1 score per trace from the observed output field."""
    traces = [
        _trace(
            f"{id_offset + i:032x}",
            [_span("a1", "llm", attrs={"output.value": obs})],
        )
        for i, obs in enumerate(observed)
    ]
    return evaluate_traces(traces, F1_SPEC, dataset=dataset)


def _delta(result: dict[str, Any], key: str) -> dict[str, Any]:
    rows = {
        f"{d['evaluator']}/{d['metric']}": d for d in result["deltas"]
    }
    return rows[key]


# -- Wilson score interval ---------------------------------------------------


def test_wilson_ci_known_values() -> None:
    """Hand-computed Wilson bounds: n=100, z=1.96."""
    lo, hi = _wilson_ci(0.5, 100) or (math.nan, math.nan)
    assert lo == pytest.approx(0.403828, abs=1e-4)
    assert hi == pytest.approx(0.596172, abs=1e-4)

    lo, hi = _wilson_ci(0.9, 100) or (math.nan, math.nan)
    assert lo == pytest.approx(0.825633, abs=1e-4)
    assert hi == pytest.approx(0.944771, abs=1e-4)


def test_wilson_ci_edge_and_empty() -> None:
    """p at the boundaries stays inside [0,1]; no denominator → None."""
    lo, hi = _wilson_ci(1.0, 20) or (math.nan, math.nan)
    assert hi == 1.0
    assert 0.0 < lo < 1.0  # ~0.83 — never collapses to the point
    lo, hi = _wilson_ci(0.0, 20) or (math.nan, math.nan)
    assert lo == 0.0
    assert 0.0 < hi < 1.0
    assert _wilson_ci(0.5, 0) is None
    assert _wilson_ci(0.5, -3) is None


# -- binary metrics -----------------------------------------------------------


def test_binary_ztest_known_cases() -> None:
    """50/100 vs 90/100 separates; 51/100 vs 52/100 does not."""
    out = compare_runs(_binary_report(50, 100, "a", 0),
                       _binary_report(90, 100, "b", 1000))
    d = _delta(out, "error_free/no_error_spans")
    assert d["test"] == "two_proportion_z"
    assert d["significance"] == "significant"
    assert d["p_value"] is not None
    assert d["p_value"] < 0.05

    out = compare_runs(_binary_report(51, 100, "a", 0),
                       _binary_report(52, 100, "b", 1000))
    d = _delta(out, "error_free/no_error_spans")
    assert d["significance"] == "not_significant"
    assert d["p_value"] is not None
    assert d["p_value"] > 0.05


def test_binary_ci_fields_reported() -> None:
    out = compare_runs(_binary_report(50, 100, "a", 0),
                       _binary_report(90, 100, "b", 1000))
    d = _delta(out, "error_free/no_error_spans")
    for ci_key, rate_key in (("ci_a", "pass_rate_a"),
                             ("ci_b", "pass_rate_b")):
        lo, hi = d[ci_key]
        assert 0.0 <= lo <= d[rate_key] <= hi <= 1.0
    assert d["ci_a"] == pytest.approx([0.403828, 0.596172], abs=1e-4)
    assert d["ci_b"] == pytest.approx([0.825633, 0.944771], abs=1e-4)


def test_binary_ci_under_min_sample_is_honestly_wide() -> None:
    """n=3/side refuses the test but still reports a descriptive CI —
    Wilson is valid at small n, and honestly wide there."""
    out = compare_runs(_binary_report(3, 3, "a", 0),
                       _binary_report(3, 3, "b", 1000))
    d = _delta(out, "error_free/no_error_spans")
    assert d["significance"] == "insufficient_data"
    assert d["p_value"] is None
    assert d["ci_a"] == pytest.approx([0.438494, 1.0], abs=1e-4)
    assert d["ci_b"] == pytest.approx([0.438494, 1.0], abs=1e-4)


def test_binary_missing_side_no_ci() -> None:
    """Metric observed only on side a — the absent side gets no CI
    and no pass rate, never a fabricated 0."""
    out = compare_runs(_binary_report(10, 10, "a", 0),
                       _f1_report(["alpha"] * 10, "b", 1000))
    d = _delta(out, "error_free/no_error_spans")
    assert d["significance"] == "insufficient_data"
    assert d["pass_rate_b"] is None
    assert d["ci_b"] is None
    assert d["ci_a"] is not None


# -- continuous metrics (Mann-Whitney U) --------------------------------------


def test_mann_whitney_known_cases() -> None:
    """Identical samples → u at the mean → p=1; fully separated → tiny p."""
    xs = [0.4, 0.666667, 1.0, 0.5, 0.0] * 2
    u, p = _mann_whitney(xs, list(xs)) or (math.nan, math.nan)
    assert u == pytest.approx(50.0)  # n1*n2/2 — dead center
    assert p == pytest.approx(1.0)

    u, p = _mann_whitney([1.0] * 10, [2.0] * 10) or (math.nan, math.nan)
    assert u == 0.0
    assert p < 0.001

    # all pooled values tied → variance 0 → undefined, not noise
    assert _mann_whitney([0.5] * 10, [0.5] * 10) is None
    assert _mann_whitney([], [1.0] * 10) is None


def test_continuous_same_distribution_not_significant() -> None:
    """Identical score multisets on both sides → u at the mean → p=1."""
    observed = ["alpha beta gamma delta", "alpha beta", "omega zeta",
                "alpha", "alpha omega"] * 3  # 15 traces, varied scores
    out = compare_runs(_f1_report(observed, "a", 0),
                       _f1_report(list(observed), "b", 1000))
    d = _delta(out, "token_f1/field_token_f1")
    assert d["test"] == "mann_whitney_u"
    assert d["significance"] == "not_significant"
    assert d["p_value"] == pytest.approx(1.0)
    assert d["ci_a"] is None
    assert d["ci_b"] is None


def test_continuous_separated_is_significant() -> None:
    """All-fail vs all-pass continuous scores — rank separation is
    total even though within-side variance is zero."""
    out = compare_runs(
        _f1_report(["omega zeta"] * 15, "a", 0),           # f1 = 0.0
        _f1_report(["alpha beta gamma delta"] * 15, "b", 1000),  # f1 = 1.0
    )
    d = _delta(out, "token_f1/field_token_f1")
    assert d["significance"] == "significant"
    assert d["p_value"] is not None
    assert d["p_value"] < 0.05
    assert d["delta"] == pytest.approx(1.0)


def test_continuous_insufficient_data() -> None:
    """Same floor as the z-test: <10 scored results/side → refused."""
    out = compare_runs(_f1_report(["omega zeta"] * 5, "a", 0),
                       _f1_report(["alpha beta gamma delta"] * 5, "b", 1000))
    d = _delta(out, "token_f1/field_token_f1")
    assert d["test"] == "mann_whitney_u"
    assert d["significance"] == "insufficient_data"
    assert d["p_value"] is None


def test_continuous_degenerate_not_computable() -> None:
    """Every pooled score identical → ranks carry no information —
    undefined, distinct from merely underpowered."""
    out = compare_runs(_f1_report(["alpha beta"] * 12, "a", 0),
                       _f1_report(["alpha beta"] * 12, "b", 1000))
    d = _delta(out, "token_f1/field_token_f1")
    assert d["significance"] == "not_computable"
    assert d["p_value"] is None


def test_unknown_evaluator_remains_not_tested() -> None:
    """An unregistered evaluator's score semantics are unverified —
    no test family is claimed for it."""
    spec = [{"name": "bogus"}]
    a = evaluate_traces([_trace(f"{i:032x}", [_span("a1", "x")])
                         for i in range(3)], spec, dataset="a")
    b = evaluate_traces([_trace(f"{i + 100:032x}", [_span("a1", "x")])
                         for i in range(3)], spec, dataset="b")
    d = _delta(compare_runs(a, b), "bogus/unknown_evaluator")
    assert d["significance"] == "not_tested"
    assert d["test"] is None
    assert d["p_value"] is None


# -- multiple comparisons ------------------------------------------------------


def test_multiple_comparisons_note() -> None:
    """k>1 tested metrics → an honest unadjusted-family warning; a
    single test → no note."""
    observed = ["alpha beta gamma delta", "alpha beta", "omega zeta",
                "alpha", "alpha omega"] * 3
    out = compare_runs(
        evaluate_traces(
            [_trace(f"{i:032x}", [_span("a1", "op", error=i >= 50),
                                  _span("b2", "llm", attrs={
                                      "output.value": observed[i % 15]})])
             for i in range(100)],
            BINARY_SPEC + F1_SPEC, dataset="a"),
        evaluate_traces(
            [_trace(f"{i + 1000:032x}",
                    [_span("a1", "op", error=i >= 90),
                     _span("b2", "llm", attrs={
                         "output.value": observed[i % 15]})])
             for i in range(100)],
            BINARY_SPEC + F1_SPEC, dataset="b"),
    )
    assert out["multiple_comparisons"] == (
        "2 tests at alpha=0.05 unadjusted — interpret jointly with care"
    )

    single = compare_runs(_binary_report(50, 100, "a", 0),
                          _binary_report(90, 100, "b", 1000))
    assert single["multiple_comparisons"] is None


# -- gate ----------------------------------------------------------------------


def test_gate_binary_regression_still_fails() -> None:
    """90% → 50% pass-rate drop is a significant regression — the gate
    catches it with the same evidence fields as before."""
    result = compare_runs(_binary_report(90, 100, "a", 0),
                          _binary_report(50, 100, "b", 1000))
    gate = apply_gate(result, {"fail_on_significant_regression": True})
    assert gate["gate_passed"] is False
    f = gate["failures"][0]
    assert f["metric"] == "error_free/no_error_spans"
    assert f["reason"] == "statistically significant regression"
    assert f["delta"] < 0
    assert f["p_value"] < 0.05


def test_gate_continuous_regression_now_testable() -> None:
    """A significant Mann-Whitney regression with negative mean delta
    reaches fail_on_significant_regression — previously continuous
    metrics were never tested at all."""
    result = compare_runs(
        _f1_report(["alpha beta gamma delta"] * 15, "a", 0),
        _f1_report(["omega zeta"] * 15, "b", 1000),
    )
    gate = apply_gate(result, {"fail_on_significant_regression": True})
    assert gate["gate_passed"] is False
    assert gate["failures"][0]["metric"] == "token_f1/field_token_f1"


def test_gate_improvement_and_untested_do_not_fail() -> None:
    """Significant improvement (delta > 0) and insufficient_data are
    never gate failures."""
    improved = compare_runs(_binary_report(50, 100, "a", 0),
                            _binary_report(90, 100, "b", 1000))
    assert apply_gate(
        improved, {"fail_on_significant_regression": True}
    )["gate_passed"] is True

    tiny = compare_runs(_binary_report(1, 3, "a", 0),
                        _binary_report(3, 3, "b", 1000))
    assert apply_gate(
        tiny, {"fail_on_significant_regression": True}
    )["gate_passed"] is True
