"""Operational alert engine — Cycle 07 §07.5.

Rules are pure threshold predicates over store aggregates; evaluation
is deterministic given the store contents (no wall clock — the caller
passes the window end). Persisted rules live in a JSON file under the
data dir so a local install needs no extra machinery.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge_lens.storage.base import TraceStore

# metric → how to extract it from the aggregates row
_METRICS = (
    "span_count", "error_count", "error_rate", "trace_count",
    "avg_duration_ms", "total_cost_usd", "loop_findings",
)


def load_rules(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("rules", [])


def save_rule(path: Path, rule: dict[str, Any]) -> list[dict[str, Any]]:
    rules = [r for r in load_rules(path) if r.get("rule_id") != rule["rule_id"]]
    rules.append(rule)
    rules.sort(key=lambda r: r["rule_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2, sort_keys=True), encoding="utf-8")
    return rules


def _aggregate(store: TraceStore, project_id: str | None) -> dict[str, float]:
    """Aggregate signals over stored traces — reads only, no clock."""
    stats = store.stats()
    spans_total = int(stats.get("spans") or 0)
    errors = int(stats.get("error_spans") or 0)
    return {
        "span_count": float(spans_total),
        "trace_count": float(stats.get("traces") or 0),
        "error_count": float(errors),
        "error_rate": errors / spans_total if spans_total else 0.0,
        "avg_duration_ms": 0.0,   # honest: duration aggregation not computed here
        "total_cost_usd": 0.0,    # cost aggregate requires pricing — filled by caller
        "loop_findings": 0.0,     # filled by caller when computed
    }


def evaluate_rules(
    rules: list[dict[str, Any]],
    metrics: dict[str, float],
) -> dict[str, Any]:
    """Evaluate threshold rules → fired events with evidence."""
    fired: list[dict[str, Any]] = []
    for rule in sorted(rules, key=lambda r: str(r.get("rule_id", ""))):
        metric = str(rule.get("metric", ""))
        op = str(rule.get("op", ">"))
        raw_thr = rule.get("threshold")
        if not isinstance(raw_thr, (int, float, str)):
            continue
        try:
            threshold = float(raw_thr)
        except (TypeError, ValueError):
            continue
        observed = metrics.get(metric)
        if observed is None:
            continue
        hit = (
            (op == ">" and observed > threshold)
            or (op == ">=" and observed >= threshold)
            or (op == "<" and observed < threshold)
            or (op == "<=" and observed <= threshold)
            or (op == "==" and observed == threshold)
        )
        if hit:
            fired.append({
                "rule_id": rule.get("rule_id"),
                "metric": metric, "op": op,
                "threshold": threshold, "observed": observed,
                "severity": rule.get("severity", "warning"),
                "message": rule.get("message")
                or f"{metric} {op} {threshold} (observed {observed:.4g})",
            })
    return {"fired": fired, "evaluated": len(rules)}
