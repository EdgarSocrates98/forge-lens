"""Operational alert engine — Cycle 07 §07.5.

Rules are pure threshold predicates over scoped aggregates; evaluation
is deterministic given the metrics map. Rules may be persisted in a
JSON file via ``save_rule``/``load_rules`` (data-dir local, no service).

Metric resolution (gap §07.5 follow-up): ``resolve_metrics`` maps each
name in ``METRICS`` to a value computed from store data at call time —
no wall-clock anywhere. Two scopes exist, and each resolver's docstring
says which it uses:

* **store scope** — ``store.stats(project_id)`` covers the whole stored
  corpus for the project; the window the four counter metrics have
  always had.
* **recent-trace window** — duration/cost/token/loop metrics need
  per-trace data the stats roll-up does not carry, so they resolve over
  the ``METRIC_WINDOW`` most recent traces by ``started_at`` (the same
  bounded convention as ``/v1/ops/summary``), never an unbounded fleet
  scan. Loop detection is bounded by construction: at most
  ``METRIC_WINDOW`` traces are evaluated per call.

``eval_pass_rate`` is deliberately NOT offered: ``/v1/eval/run``
reports are ephemeral (never persisted) and persisted
``experiment_results`` rows are per-experiment, two-cohort compare
deltas — a single project-scoped scalar would need a rule-level
experiment/cohort/evaluator selector the rule schema cannot express.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from forge_lens.contracts.v1.trace import Trace
from forge_lens.intel.cost import CostReport, extract_usage, trace_cost
from forge_lens.intel.pricing import PricingRegistry
from forge_lens.ops.loops import detect_loops
from forge_lens.storage.base import TraceStore

METRIC_WINDOW = 200
"""Recent-trace window for derived metrics — bounded like ``/v1/ops/summary``."""

_WINDOW_CAP = 500
"""Hard ceiling on ``window`` — same 500 bound other roll-ups clamp to."""


@dataclass(frozen=True)
class _Window:
    """One materialized metric window — fetched once per evaluation.

    ``cost_reports`` is ``None`` when no ``PricingRegistry`` was
    supplied: cost metrics then resolve to *absent*, not zero — a fake
    zero could silence a rule that should have fired.
    """

    stats: dict[str, Any]
    trace_rows: int
    """Traces listed in the recent window (denominator for *_rate)."""
    error_traces: int
    """Of them, how many carry stored ``status == 'error'``."""
    durations_ms: list[float]
    """``ended_at - started_at`` per *ended* window trace, sorted ASC."""
    token_total: int
    cost_reports: tuple[CostReport, ...] | None
    loop_traces: int


# -- resolvers: metric name -> fn(_Window) -> float | None ------------------
# ``None`` means "not resolvable here" and the metric is OMITTED from the
# map (evaluate_rules skips unknown metrics) — never a fabricated value.


def _r_span_count(w: _Window) -> float:
    """``span_count`` — stored spans, whole corpus (store scope)."""
    return float(int(w.stats.get("spans") or 0))


def _r_trace_count(w: _Window) -> float:
    """``trace_count`` — stored traces, whole corpus (store scope)."""
    return float(w.stats.get("traces") or 0)


def _r_error_count(w: _Window) -> float:
    """``error_count`` — spans with status error (store scope)."""
    return float(int(w.stats.get("error_spans") or 0))


def _r_error_rate(w: _Window) -> float:
    """``error_rate`` — error_spans / spans, 0.0 on empty store."""
    spans = int(w.stats.get("spans") or 0)
    errors = int(w.stats.get("error_spans") or 0)
    return errors / spans if spans else 0.0


def _r_avg_duration_ms(w: _Window) -> float:
    """``avg_duration_ms`` — mean trace duration in milliseconds over
    *ended* window traces; 0.0 when none in the window has ended (a
    still-running trace has no observable duration — excluded, not 0)."""
    n = len(w.durations_ms)
    return sum(w.durations_ms) / n if n else 0.0


def _r_max_duration_ms(w: _Window) -> float:
    """``max_duration_ms`` — slowest ended window trace in ms."""
    return w.durations_ms[-1] if w.durations_ms else 0.0


def _r_p95_duration_ms(w: _Window) -> float:
    """``p95_duration_ms`` — nearest-rank 95th percentile of ended window
    trace durations in ms. The store computes no percentiles, so this is
    a window rank, not a fleet-wide quantile."""
    n = len(w.durations_ms)
    return w.durations_ms[math.ceil(0.95 * n) - 1] if n else 0.0


def _r_error_traces(w: _Window) -> float:
    """``error_traces`` — window traces whose stored status is error.
    Trace-level failure count, unlike ``error_count`` (spans)."""
    return float(w.error_traces)


def _r_error_trace_rate(w: _Window) -> float:
    """``error_trace_rate`` — error_traces / window traces; 0.0 on an
    empty window. Trace-level, unlike ``error_rate`` (span-level)."""
    return w.error_traces / w.trace_rows if w.trace_rows else 0.0


def _r_token_total(w: _Window) -> float:
    """``token_total`` — sum of observed token usage across all spans of
    all window traces (``total_tokens`` with the input+output fallback,
    identical to ``intel.cost.trace_cost``). Unit: tokens."""
    return float(w.token_total)


def _r_cost_total_usd(w: _Window) -> float | None:
    """``cost_total_usd`` — estimated spend (USD) summed over window
    traces via ``trace_cost``. Usage priced by absent registry models is
    *excluded* — see ``unpriced_model_count``. None without a registry."""
    if w.cost_reports is None:
        return None
    return round(sum(r.total_estimated_usd for r in w.cost_reports), 8)


def _r_cost_per_trace_usd(w: _Window) -> float | None:
    """``cost_per_trace_usd`` — ``cost_total_usd`` divided by the window
    trace count; unpriced usage stays excluded from the numerator."""
    if w.cost_reports is None:
        return None
    if not w.trace_rows:
        return 0.0
    return sum(r.total_estimated_usd for r in w.cost_reports) / w.trace_rows


def _r_unpriced_model_count(w: _Window) -> float | None:
    """``unpriced_model_count`` — distinct models in the window carrying
    usage but no registry price — the blind-spend companion to the cost
    metrics. None without a registry."""
    if w.cost_reports is None:
        return None
    return float(
        len({m for r in w.cost_reports for m in r.unpriced_models})
    )


def _r_loop_traces(w: _Window) -> float:
    """``loop_traces`` — window traces where ``detect_loops`` yields ≥1
    finding. Bounded: at most ``METRIC_WINDOW`` traces are evaluated."""
    return float(w.loop_traces)


_Resolver = Callable[[_Window], float | None]

_STATS_RESOLVERS: dict[str, _Resolver] = {
    "error_count": _r_error_count,
    "error_rate": _r_error_rate,
    "span_count": _r_span_count,
    "trace_count": _r_trace_count,
}

_WINDOW_RESOLVERS: dict[str, _Resolver] = {
    "avg_duration_ms": _r_avg_duration_ms,
    "cost_per_trace_usd": _r_cost_per_trace_usd,
    "cost_total_usd": _r_cost_total_usd,
    "error_trace_rate": _r_error_trace_rate,
    "error_traces": _r_error_traces,
    "loop_traces": _r_loop_traces,
    "max_duration_ms": _r_max_duration_ms,
    "p95_duration_ms": _r_p95_duration_ms,
    "token_total": _r_token_total,
    "unpriced_model_count": _r_unpriced_model_count,
}

_RESOLVERS: dict[str, _Resolver] = {**_STATS_RESOLVERS, **_WINDOW_RESOLVERS}

# metrics the API endpoint can resolve (names a rule may reference)
METRICS: tuple[str, ...] = tuple(sorted(_RESOLVERS))


def resolve_metrics(
    store: TraceStore,
    project_id: str | None = None,
    registry: PricingRegistry | None = None,
    *,
    window: int = METRIC_WINDOW,
    only: set[str] | None = None,
) -> dict[str, float]:
    """Resolve metric names → values for ``evaluate_rules``.

    ``only`` restricts resolution to the given names — callers that
    know their rules' metrics skip the trace scan entirely when no
    window metric is requested. Names outside ``METRICS`` are ignored,
    matching ``evaluate_rules``' unknown-means-never-fires contract.
    """
    wanted = set(_RESOLVERS) if only is None else (set(only) & set(_RESOLVERS))
    stats = store.stats(project_id=project_id)
    w = _Window(
        stats=stats,
        trace_rows=0,
        error_traces=0,
        durations_ms=[],
        token_total=0,
        cost_reports=() if registry is not None else None,
        loop_traces=0,
    )
    if wanted & set(_WINDOW_RESOLVERS):
        try:
            limit = max(0, min(int(window), _WINDOW_CAP))
        except (TypeError, ValueError, OverflowError):
            limit = METRIC_WINDOW
        w = _build_window(store, project_id, registry, stats, limit)
    out: dict[str, float] = {}
    for name in sorted(wanted):
        value = _RESOLVERS[name](w)
        if value is not None:
            out[name] = value
    return out


def _build_window(
    store: TraceStore,
    project_id: str | None,
    registry: PricingRegistry | None,
    stats: dict[str, Any],
    limit: int,
) -> _Window:
    """Scan at most ``limit`` recent traces once; derive every window
    aggregate in a single pass (one ``get_trace`` per row)."""
    rows = store.list_traces(project_id=project_id, limit=limit)
    durations: list[float] = []
    error_traces = 0
    token_total = 0
    cost_reports: list[CostReport] = []
    loop_traces = 0
    for row in rows:
        if row.get("status") == "error":
            error_traces += 1
        # fetch the copy this row actually names — the same trace_id may
        # exist under several projects when project_id=None
        row_pid = row.get("project_id")
        trace: Trace | None = store.get_trace(
            str(row.get("trace_id", "")),
            project_id=row_pid if isinstance(row_pid, str) else None,
        )
        if trace is None:
            # deleted between list and fetch — skip; never invent it
            continue
        if trace.ended_at is not None:
            durations.append(
                (trace.ended_at - trace.started_at).total_seconds() * 1000.0
            )
        for span in trace.spans:
            usage = extract_usage(span)
            if usage is not None:
                # same total fallback as intel.cost.trace_cost
                token_total += usage.total_tokens or (
                    (usage.input_tokens or 0) + (usage.output_tokens or 0)
                )
        if registry is not None:
            cost_reports.append(trace_cost(trace, registry))
        if detect_loops(trace)["findings"]:
            loop_traces += 1
    durations.sort()
    return _Window(
        stats=stats,
        trace_rows=len(rows),
        error_traces=error_traces,
        durations_ms=durations,
        token_total=token_total,
        cost_reports=tuple(cost_reports) if registry is not None else None,
        loop_traces=loop_traces,
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
