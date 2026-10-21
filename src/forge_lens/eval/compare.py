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
