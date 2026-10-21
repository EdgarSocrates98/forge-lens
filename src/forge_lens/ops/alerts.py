"""Operational alert engine — Cycle 07 §07.5.

Rules are pure threshold predicates over scoped aggregates; evaluation
is deterministic given the metrics map. Rules may be persisted in a
JSON file via ``save_rule``/``load_rules`` (data-dir local, no service).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# metrics the API endpoint computes (scoped by project)
METRICS = (
    "span_count", "error_count", "error_rate", "trace_count",
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
