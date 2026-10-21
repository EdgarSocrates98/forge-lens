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


def _valid_rule(r: Any) -> bool:
    return isinstance(r, dict) and isinstance(r.get("rule_id"), str)


def load_rules(path: Path) -> list[dict[str, Any]]:
    """Tolerant read: a truncated/corrupt file yields [] (never a
    crash that also blocks repair via save_rule)."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        inner = data.get("rules")
        raw = inner if isinstance(inner, list) else []
    else:  # scalars/null — not a rules document at all
        return []
    return [r for r in raw if _valid_rule(r)]


def save_rule(path: Path, rule: dict[str, Any]) -> list[dict[str, Any]]:
    """Insert/replace a rule; atomic write (tmp+rename) so a crash
    mid-write can't truncate the store."""
    rules = [r for r in load_rules(path) if r["rule_id"] != rule["rule_id"]]
    rules.append(rule)
    rules.sort(key=lambda r: r["rule_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rules, indent=2, sort_keys=True),
                   encoding="utf-8")
    tmp.replace(path)
    return rules


def delete_rule(path: Path, rule_id: str) -> list[dict[str, Any]]:
    rules = [r for r in load_rules(path) if r["rule_id"] != rule_id]
    if path.exists():
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(rules, indent=2, sort_keys=True),
                       encoding="utf-8")
        tmp.replace(path)
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
