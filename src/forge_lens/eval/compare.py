"""A/B comparison of two EvalReports — honest statistical claims only.

Binary metrics (score ∈ {0,1}) get a two-proportion z-test; continuous
metrics report observed deltas and are explicitly marked as untested —
we never print a p-value we didn't compute.
"""

from __future__ import annotations

import math
from typing import Any

from forge_lens.contracts.base import LensModel
from forge_lens.eval.evaluators import EVALUATORS
from forge_lens.eval.runner import EvalReport

# two-sided normal cutoff for alpha=0.05
_Z_CRIT = 1.96
# below this many scored samples a proportion test is noise
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
    significance: str = "not_tested"
    """``significant`` | ``not_significant`` | ``insufficient_data``
    (n < 10/side) | ``not_computable`` (zero pooled variance — the test
    is undefined, not merely underpowered) | ``not_tested`` (continuous
    metric — no p-value is fabricated)."""
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


def compare_runs(a: EvalReport, b: EvalReport) -> dict[str, Any]:
    """Compare baseline ``a`` against candidate ``b`` per metric.

    Keys are ``evaluator/metric``; significance is computed only for
    binary evaluators with ≥ ``_MIN_SAMPLE`` scored results on both
    sides — otherwise the delta stands alone, honestly labeled.
    """
    keys = sorted(set(a.summary) | set(b.summary))
    deltas: list[MetricDelta] = []
    for key in keys:
        evaluator = key.split("/", 1)[0]
        sa = a.summary.get(key, {})
        sb = b.summary.get(key, {})
        n_a = int(sa.get("n") or 0)
        n_b = int(sb.get("n") or 0)
        mean_a = sa.get("mean_score")
        mean_b = sb.get("mean_score")
        row = MetricDelta(
            evaluator=evaluator,
            metric=key.split("/", 1)[1],
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
        binary = EVALUATORS.get(evaluator, (None, False))[1]
        if not binary:
            row.significance = "not_tested"
        elif n_a < _MIN_SAMPLE or n_b < _MIN_SAMPLE:
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
        deltas.append(row)
    return {
        "a": {"dataset": a.dataset, "trace_ids": a.trace_ids, "n": len(a.trace_ids)},
        "b": {"dataset": b.dataset, "trace_ids": b.trace_ids, "n": len(b.trace_ids)},
        "deltas": [d.model_dump(mode="json") for d in deltas],
    }
