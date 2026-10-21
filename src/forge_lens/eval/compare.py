"""A/B comparison of two EvalReports — honest statistical claims only.

Binary metrics (score ∈ {0,1}) get a two-proportion z-test on their
pass rate plus a Wilson score interval (95%) on each side — Wilson
keeps coverage honest at small n and near the 0/1 boundaries where a
normal approximation breaks down. Continuous metrics get a
Mann-Whitney U test over the per-result score samples (rank-based,
distribution-free — honest for bounded/skewed scores where a t-test's
normality assumption would be fabricated). We never print a p-value
we didn't compute.
"""

from __future__ import annotations

import math
from typing import Any

from forge_lens.contracts.base import LensModel
from forge_lens.eval.evaluators import EVALUATORS
from forge_lens.eval.runner import EvalReport

# two-sided normal cutoff for alpha=0.05
_Z_CRIT = 1.96
_ALPHA = 0.05
# below this many scored samples per side a test is noise
_MIN_SAMPLE = 10


class MetricDelta(LensModel):
    evaluator: str
    metric: str
    n_a: int
    n_b: int
    mean_a: float | None = None
    mean_b: float | None = None
    delta: float | None = None
    pass_rate_a: float | None = None
    pass_rate_b: float | None = None
    test: str | None = None
    """The test family this metric's semantics select —
    ``two_proportion_z`` for binary pass rates, ``mann_whitney_u`` for
    continuous score distributions; ``None`` for unregistered
    evaluators whose score meaning is unverified. Set whenever the
    evaluator is known even when the run is refused
    (``insufficient_data``/``not_computable``)."""
    ci_a: tuple[float, float] | None = None
    ci_b: tuple[float, float] | None = None
    """Wilson score interval ``[lo, hi]`` at 95% for ``pass_rate_a`` /
    ``pass_rate_b`` — binary metrics only. Descriptive, not a test:
    reported even below ``_MIN_SAMPLE``, where it is honestly wide."""
    significance: str = "not_tested"
    """``significant`` | ``not_significant`` | ``insufficient_data``
    (n < 10/side) | ``not_computable`` (zero variance — the test is
    undefined, not merely underpowered) | ``not_tested`` (evaluator
    not in the registry — score semantics unverified)."""
    p_value: float | None = None


def _z_test(p1: float, n1: int, p2: float, n2: int) -> tuple[float, float] | None:
    """Two-proportion z-test → (z, p). None when the test is undefined."""
    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    var = pooled * (1 - pooled) * (1 / n1 + 1 / n2)
    if var <= 0:
        return None  # identical proportions or degenerate samples
    z = (p1 - p2) / math.sqrt(var)
    # two-sided p from the normal CDF
    p = math.erfc(abs(z) / math.sqrt(2))
    return z, p


def _wilson_ci(p: float, n: int) -> tuple[float, float] | None:
    """Wilson score interval ``[lo, hi]`` for a binomial proportion.

    Better coverage than the normal approximation at small n and near
    the 0/1 edges — it never emits a bound outside [0, 1]. None when
    there is no denominator (n ≤ 0)."""
    if n <= 0:
        return None
    z2 = _Z_CRIT * _Z_CRIT
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    margin = (_Z_CRIT / denom) * math.sqrt(
        p * (1 - p) / n + z2 / (4 * n * n)
    )
    return (
        round(max(0.0, center - margin), 6),
        round(min(1.0, center + margin), 6),
    )


def _score_samples(report: EvalReport, key: str) -> list[float]:
    """Per-result non-null scores for ``evaluator/metric`` — the raw
    evidence a rank test needs that summary aggregates don't carry.

    Results matching the metric on the same trace each count (a spec
    may repeat one evaluator with different params), matching the
    summary's ``n`` — they are not strictly independent in that case.
    """
    evaluator, _, metric = key.partition("/")
    out: list[float] = []
    for tid in sorted(report.results):
        for r in report.results[tid]:
            if (
                r.evaluator == evaluator
                and r.metric == metric
                and r.score is not None
            ):
                out.append(r.score)
    return sorted(out)


def _mann_whitney(xs: list[float], ys: list[float]) -> tuple[float, float] | None:
    """Two-sided Mann-Whitney U → ``(u, p)``, normal approximation
    with continuity and tie corrections. None when undefined (zero
    variance — every pooled observation tied).

    Rank-based and distribution-free: appropriate for bounded/skewed
    eval scores. With the ``_MIN_SAMPLE`` floor (≥10/side) the
    asymptotic p is standard practice; smaller samples are refused as
    ``insufficient_data`` rather than approximated silently. Strictly
    it detects distribution shift (stochastic ordering), not a mean or
    median delta — direction for gating comes from the observed mean
    ``delta``, documented in the guide.
    """
    n1, n2 = len(xs), len(ys)
    if n1 == 0 or n2 == 0:
        return None
    pooled = sorted([(v, 0) for v in xs] + [(v, 1) for v in ys])
    n = len(pooled)
    ranks = [0.0] * n
    tie_counts: list[int] = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pooled[j + 1][0] == pooled[i][0]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based average rank
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        if j > i:
            tie_counts.append(j - i + 1)
        i = j + 1
    r1 = sum(ranks[k] for k in range(n) if pooled[k][1] == 0)
    u1 = r1 - n1 * (n1 + 1) / 2.0
    mean_u = n1 * n2 / 2.0
    # tie-corrected variance (Siegel & Castellan) — each group of t
    # equal values shrinks the variance by (t^3 - t) / (n(n - 1))
    tie_term = sum(t * t * t - t for t in tie_counts)
    var = (n1 * n2 / 12.0) * ((n + 1) - tie_term / (n * (n - 1)))
    if var <= 0:
        return None  # all tied — ranks carry no information
    diff = u1 - mean_u
    # continuity correction toward the mean
    z = 0.0 if diff == 0 else (
        (diff - math.copysign(0.5, diff)) / math.sqrt(var)
    )
    p = math.erfc(abs(z) / math.sqrt(2))
    return u1, p


def compare_runs(a: EvalReport, b: EvalReport) -> dict[str, Any]:
    """Compare baseline ``a`` against candidate ``b`` per metric.

    Keys are ``evaluator/metric``. Binary evaluators get a
    two-proportion z-test plus Wilson CIs; non-binary evaluators get a
    Mann-Whitney U over raw score samples — both only with ≥
    ``_MIN_SAMPLE`` scored results on both sides. Otherwise the delta
    stands alone, honestly labeled. ``multiple_comparisons`` warns
    when more than one metric was tested: k unadjusted tests at
    alpha=0.05 must be interpreted jointly with care.
    """
    keys = sorted(set(a.summary) | set(b.summary))
    deltas: list[MetricDelta] = []
    for key in keys:
        evaluator, _, metric = key.partition("/")
        sa = a.summary.get(key, {})
        sb = b.summary.get(key, {})
        n_a = int(sa.get("n") or 0)
        n_b = int(sb.get("n") or 0)
        mean_a = sa.get("mean_score")
        mean_b = sb.get("mean_score")
        row = MetricDelta(
            evaluator=evaluator,
            metric=metric,
            n_a=n_a,
            n_b=n_b,
            mean_a=mean_a,
            mean_b=mean_b,
            delta=(
                round(mean_b - mean_a, 6)
                if mean_a is not None and mean_b is not None
                else None
            ),
            pass_rate_a=sa.get("pass_rate"),
            pass_rate_b=sb.get("pass_rate"),
        )
        entry = EVALUATORS.get(evaluator)
        if entry is None:
            # unregistered evaluator — its score semantics are
            # unverified, so no test family is claimed for it
            row.significance = "not_tested"
        elif entry[1]:  # binary — pass-rate proportion test
            row.test = "two_proportion_z"
            if row.pass_rate_a is not None:
                row.ci_a = _wilson_ci(row.pass_rate_a, n_a)
            if row.pass_rate_b is not None:
                row.ci_b = _wilson_ci(row.pass_rate_b, n_b)
            if n_a < _MIN_SAMPLE or n_b < _MIN_SAMPLE:
                row.significance = "insufficient_data"
            else:
                res = _z_test(
                    row.pass_rate_a or 0.0, n_a, row.pass_rate_b or 0.0, n_b
                )
                if res is None:
                    row.significance = "not_computable"
                else:
                    row.p_value = round(res[1], 6)
                    row.significance = (
                        "not_significant" if abs(res[0]) < _Z_CRIT else "significant"
                    )
        else:  # continuous — rank test over the raw score samples
            row.test = "mann_whitney_u"
            xs = _score_samples(a, key)
            ys = _score_samples(b, key)
            if len(xs) < _MIN_SAMPLE or len(ys) < _MIN_SAMPLE:
                row.significance = "insufficient_data"
            else:
                res = _mann_whitney(xs, ys)
                if res is None:
                    row.significance = "not_computable"
                else:
                    row.p_value = round(res[1], 6)
                    row.significance = (
                        "significant" if res[1] < _ALPHA else "not_significant"
                    )
        deltas.append(row)
    tested = sum(1 for d in deltas if d.p_value is not None)
    return {
        "a": {"dataset": a.dataset, "trace_ids": a.trace_ids, "n": len(a.trace_ids)},
        "b": {"dataset": b.dataset, "trace_ids": b.trace_ids, "n": len(b.trace_ids)},
        "deltas": [d.model_dump(mode="json") for d in deltas],
        # honest multiplicity note — no correction is applied; when k>1
        # tests ran, the family-wise error rate exceeds alpha
        "multiple_comparisons": (
            f"{tested} tests at alpha={_ALPHA} unadjusted — "
            "interpret jointly with care"
            if tested > 1
            else None
        ),
    }


def apply_gate(result: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a regression gate over a compare result.

    Gate spec::

        {
          "metrics": {
            "error_free/no_error_spans": {
              "min_pass_rate_b": 1.0,   # candidate must reach this
              "max_drop": 0.05          # pass_rate_a - pass_rate_b ≤ this
            }
          },
          "fail_on_significant_regression": true
        }

    Every failure is reported with its evidence — a gate never fails
    silently. Unknown metric keys are themselves a failure (a gate on a
    metric that wasn't evaluated is a misconfigured gate)."""
    failures: list[dict[str, Any]] = []
    by_key = {
        f"{d['evaluator']}/{d['metric']}": d for d in result.get("deltas", [])
    }
    metrics_spec = gate.get("metrics") or {}
    if not isinstance(metrics_spec, dict):
        return {
            "gate_passed": False,
            "failures": [{"reason": "gate.metrics must be an object"}],
        }
    for key, spec in sorted(metrics_spec.items()):
        if not isinstance(spec, dict):
            failures.append({"metric": key, "reason": "metric gate spec must be an object"})
            continue
        d = by_key.get(key)
        if d is None:
            failures.append({
                "metric": key,
                "reason": "metric not evaluated — cannot gate on unmeasured evidence",
            })
            continue
        min_b = spec.get("min_pass_rate_b")
        if min_b is not None:
            try:
                min_b_f = float(min_b)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{key}.min_pass_rate_b must be a number"
                ) from exc
            if not math.isfinite(min_b_f):
                raise ValueError(f"{key}.min_pass_rate_b must be finite")
            pr_b = d.get("pass_rate_b")
            if pr_b is None or pr_b < min_b_f:
                failures.append({
                    "metric": key,
                    "reason": "pass_rate_b below minimum",
                    "observed": pr_b,
                    "required": min_b_f,
                })
        max_drop = spec.get("max_drop")
        if max_drop is not None:
            try:
                max_drop_f = float(max_drop)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"{key}.max_drop must be a number"
                ) from exc
            if not math.isfinite(max_drop_f):
                raise ValueError(f"{key}.max_drop must be finite")
            pa, pb = d.get("pass_rate_a"), d.get("pass_rate_b")
            if pa is not None and pb is not None and (pa - pb) > max_drop_f:
                failures.append({
                    "metric": key,
                    "reason": "pass_rate dropped more than allowed",
                    "observed_drop": round(pa - pb, 6),
                    "max_drop": max_drop_f,
                })
    fosr = gate.get("fail_on_significant_regression")
    if fosr is not None and not isinstance(fosr, bool):
        raise ValueError("fail_on_significant_regression must be a boolean")
    if fosr:
        for d in result.get("deltas", []):
            if (
                d.get("significance") == "significant"
                and d.get("delta") is not None
                and d["delta"] < 0
            ):
                failures.append({
                    "metric": f"{d['evaluator']}/{d['metric']}",
                    "reason": "statistically significant regression",
                    "delta": d["delta"],
                    "p_value": d.get("p_value"),
                })
    return {
        "gate_passed": not failures,
        "failures": sorted(
            failures, key=lambda f: (str(f.get("metric", "")), str(f.get("reason", "")))
        ),
    }
