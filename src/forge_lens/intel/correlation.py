"""Correlation intelligence — Cycle 13 §05.5.

Pairs signals that co-occur inside one execution (``correlate_trace``)
and builds co-occurrence tables over a bounded fleet window
(``correlate_fleet``): tool ↔ failure, model ↔ failure, session
length ↔ failure, retry signals ↔ observed eval scores.

Everything here is *descriptive association*: temporal precedence
inside a trace, and rate differences across a window. Correlation is
not causation — no row claims a mechanism, and ``insufficient_data``
is the honest answer when a cohort is too small to describe.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from typing import Any

from forge_lens.contracts.v1.trace import Span, SpanKind, Trace
from forge_lens.intel.cost import extract_usage
from forge_lens.storage.base import TraceStore

_MAX_WINDOW = 500
"""Hard bound on the fleet window — same discipline as ops/summary."""

_MIN_COHORT = 5
"""Failed and ok cohorts each need ≥ this many traces before a
rate comparison is described as measured; below it the row is
``insufficient_data`` (numbers still shown, never invented)."""

_MIN_SCORED = 3
"""A retry↔eval bucket needs ≥ this many scored traces for a
mean score to be reported as more than a bare observation."""

_RETRY_ATTRS = ("gen_ai.tool.call.retry_count", "retry.count")
_MODEL_ATTRS = ("llm.model_name", "gen_ai.request.model", "gen_ai.response.model")
_SCORE_KEYS = (
    "eval.score",
    "evaluation.score",
    "forge_lens.eval.score",
    "forge_lens.score",
    "score",
)

TEMPORAL = "temporal_association"
CO_OCCURRENCE = "co_occurrence"
INSUFFICIENT = "insufficient_data"


# -- signal extraction ---------------------------------------------------


def _ordered(trace: Trace) -> list[Span]:
    """Deterministic span order — ``(start_time, span_id)``, the same
    convention as ``trace_cost``/``detect_waste``."""
    return sorted(trace.spans, key=lambda s: (s.start_time, s.span_id))


def _is_error(span: Span) -> bool:
    return span.status.code.value == "error"


def _semantic_name(span: Span) -> str:
    """``tool.name`` when observed on TOOL spans (same convention as
    the evaluators); the span name otherwise — never a guessed name."""
    if span.kind is SpanKind.TOOL:
        v = span.attributes.get("tool.name")
        if isinstance(v, str) and v:
            return v
    return span.name


def _model_of(span: Span) -> str | None:
    """Model label for an LLM span: usage record first, then the
    OpenInference/GenAI attribute keys — ``None`` stays ``None``."""
    usage = extract_usage(span)
    if usage is not None and usage.model:
        return usage.model
    for key in _MODEL_ATTRS:
        v = span.attributes.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def _declared_retries(span: Span) -> int:
    """Producer-declared retry counter — first truthy attribute wins
    (same convention as ``trace_efficiency``); unparseable → 0."""
    raw = span.attributes.get(_RETRY_ATTRS[0]) or \
        span.attributes.get(_RETRY_ATTRS[1]) or 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _eval_scores(trace: Trace) -> list[tuple[Span, float]]:
    """Numeric scores observed on EVALUATOR-kind spans.

    Recognised keys are the explicit score attributes plus any key
    ending in ``.score``; non-finite/non-numeric values are skipped —
    an absent score is evidence, never an implicit 0.
    """
    out: list[tuple[Span, float]] = []
    for s in trace.spans:
        if s.kind is not SpanKind.EVALUATOR:
            continue
        score: float | None = None
        for key in _SCORE_KEYS:
            v = s.attributes.get(key)
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)) and math.isfinite(v):
                score = float(v)
                break
        if score is None:
            for key in sorted(s.attributes):
                if not key.endswith(".score"):
                    continue
                v = s.attributes[key]
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)) and math.isfinite(v):
                    score = float(v)
                    break
        if score is not None:
            out.append((s, score))
    out.sort(key=lambda t: (t[0].start_time, t[0].span_id))
    return out


def _context_growth_edge(
    spans: list[Span],
) -> tuple[Span, Span, int] | None:
    """Largest input-token jump between consecutive LLM calls.

    Returns ``(before, after, delta)`` for the first maximal positive
    edge, else ``None`` — ties resolve to the earliest edge so output
    is deterministic.
    """
    llm_usage = [
        (s, u.input_tokens)
        for s in spans
        if s.kind is SpanKind.LLM
        and (u := extract_usage(s)) is not None
        and u.input_tokens is not None
    ]
    best: tuple[Span, Span, int] | None = None
    for (a, in_a), (b, in_b) in itertools.pairwise(llm_usage):
        delta = in_b - in_a
        if delta > 0 and (best is None or delta > best[2]):
            best = (a, b, delta)
    return best


def _evidence(signals: list[dict[str, Any]], order: dict[str, int]) -> list[str]:
    """Flattened, de-duplicated evidence span ids in trace order."""
    seen: set[str] = set()
    for sig in signals:
        seen.update(sig["span_ids"])
    return sorted(seen, key=lambda sid: (order.get(sid, len(order)), sid))


def _correlation(
    kind: str,
    signals: list[dict[str, Any]],
    strength: dict[str, Any],
    caveat: str,
    detail: str,
    order: dict[str, int],
) -> dict[str, Any]:
    return {
        "kind": kind,
        "classification": TEMPORAL,
        "signals": signals,
        "strength": strength,
        "evidence_span_ids": _evidence(signals, order),
        "caveat": caveat,
        "detail": detail,
    }


# -- per-trace -----------------------------------------------------------


def correlate_trace(trace: Trace) -> dict[str, Any]:
    """Pair co-occurring signals inside one execution.

    Every correlation names its evidence span ids and a real strength
    (counts/ratios), is labelled ``temporal_association`` — ordering by
    ``start_time`` only — and carries a caveat. A trace with nothing to
    pair returns an honest empty list plus the signal inventory.
    """
    spans = _ordered(trace)
    order = {s.span_id: i for i, s in enumerate(spans)}
    pos = {id(s): i for i, s in enumerate(spans)}
    correlations: list[dict[str, Any]] = []

    tools = [s for s in spans if s.kind is SpanKind.TOOL]
    llms = [s for s in spans if s.kind is SpanKind.LLM]
    errors = [s for s in spans if _is_error(s)]
    scored = _eval_scores(trace)

    # -- retries_preceded_error: same-name invocations before an error
    err_by_key: dict[tuple[str, str], list[Span]] = {}
    for s in errors:
        if s.kind in (SpanKind.TOOL, SpanKind.LLM):
            err_by_key.setdefault((s.kind.value, _semantic_name(s)), []).append(s)
    for (kind_val, name), group in sorted(err_by_key.items()):
        first_err_pos = min(pos[id(s)] for s in group)
        prior = [
            s for s in spans[:first_err_pos]
            if s.kind.value == kind_val and _semantic_name(s) == name
        ]
        if not prior:
            continue
        signals = [
            {"type": "prior_invocations",
             "span_ids": [s.span_id for s in prior],
             "value": len(prior)},
            {"type": "error_spans",
             "span_ids": [s.span_id for s in group],
             "value": len(group)},
        ]
        correlations.append(_correlation(
            "retries_preceded_error",
            signals,
            {
                "preceding_invocations": len(prior),
                "error_invocations": len(group),
                "declared_retry_attrs": sum(
                    _declared_retries(s) for s in prior
                ),
            },
            "same-name invocations preceding an error are candidate "
            "retries — temporal precedence is not proof of retry "
            "intent or causation",
            f"{len(prior)} {name!r} invocation(s) observed before "
            f"{len(group)} error span(s) of the same name",
            order,
        ))

    # -- tool_failure_cluster: ≥2 errored invocations of one tool
    tool_by_name: dict[str, list[Span]] = {}
    for s in tools:
        tool_by_name.setdefault(_semantic_name(s), []).append(s)
    for name in sorted(tool_by_name):
        group = tool_by_name[name]
        failed = [s for s in group if _is_error(s)]
        if len(failed) < 2:
            continue
        signals = [
            {"type": "failed_invocations",
             "span_ids": [s.span_id for s in failed],
             "value": len(failed)},
            {"type": "all_invocations",
             "span_ids": [s.span_id for s in group],
             "value": len(group)},
        ]
        correlations.append(_correlation(
            "tool_failure_cluster",
            signals,
            {
                "failed_invocations": len(failed),
                "invocations": len(group),
                "failure_share": round(len(failed) / len(group), 6),
            },
            "co-located failures in one execution — the cluster is "
            "descriptive; a shared cause is unproven",
            f"tool {name!r} failed {len(failed)} of {len(group)} "
            "invocations in this trace",
            order,
        ))

    # -- model_switch: ≥2 distinct model labels across LLM calls
    by_model: dict[str, list[str]] = {}
    unattributed: list[str] = []
    for s in llms:
        m = _model_of(s)
        if m is None:
            unattributed.append(s.span_id)
        else:
            by_model.setdefault(m, []).append(s.span_id)
    if len(by_model) >= 2:
        signals = [
            {"type": "model_calls", "value": m, "span_ids": ids}
            for m, ids in sorted(by_model.items())
        ]
        if unattributed:
            signals.append({
                "type": "model_calls",
                "value": None,
                "span_ids": unattributed,
            })
        correlations.append(_correlation(
            "model_switch",
            signals,
            {
                "distinct_models": len(by_model),
                "llm_calls": len(llms),
                "calls_without_model": len(unattributed),
            },
            "model labels are provider-reported strings — a switch is "
            "an observed label change, not proof of a different "
            "endpoint or of harm",
            f"{len(by_model)} distinct model labels across "
            f"{len(llms)} LLM calls",
            order,
        ))

    # -- context growth edges
    edge = _context_growth_edge(spans)
    if edge is not None:
        before, after, delta = edge
        after_pos = pos[id(after)]
        later_errors = [s for s in errors if pos[id(s)] > after_pos]
        # the edge exists only when both usages reported input_tokens
        u_before = extract_usage(before)
        u_after = extract_usage(after)
        growth_strength: dict[str, Any] = {
            "input_token_growth": delta,
            "from_input_tokens": (
                u_before.input_tokens if u_before is not None else None
            ),
            "to_input_tokens": (
                u_after.input_tokens if u_after is not None else None
            ),
        }
        if later_errors:
            correlations.append(_correlation(
                "context_growth_preceded_error",
                [
                    {"type": "llm_calls",
                     "span_ids": [before.span_id, after.span_id]},
                    {"type": "error_spans",
                     "span_ids": [s.span_id for s in later_errors],
                     "value": len(later_errors)},
                ],
                {**growth_strength, "error_spans_after": len(later_errors)},
                "context growth is observed between consecutive calls "
                "— the error started later in wall-clock order, which "
                "does not make the growth its cause",
                f"input context grew +{delta} tokens between "
                f"{before.span_id} and {after.span_id}; "
                f"{len(later_errors)} error span(s) started after",
                order,
            ))
        later_evals = [(s, sc) for s, sc in scored if pos[id(s)] > after_pos]
        if later_evals:
            scores = [sc for _, sc in later_evals]
            correlations.append(_correlation(
                "context_growth_preceded_eval",
                [
                    {"type": "llm_calls",
                     "span_ids": [before.span_id, after.span_id]},
                    {"type": "eval_scores",
                     "span_ids": [s.span_id for s, _ in later_evals],
                     "value": min(scores)},
                ],
                {
                    **growth_strength,
                    "eval_scores_after": len(scores),
                    "min_score": min(scores),
                    "max_score": max(scores),
                },
                "the score values are reported as observed — whether "
                "they are 'degraded' is a judgement this module does "
                "not make; precedence is not causation",
                f"input context grew +{delta} tokens before "
                f"{len(scores)} evaluator score(s) "
                f"(min {min(scores)})",
                order,
            ))

    # -- errors_preceded_eval_score: error spans before a scored eval
    if errors and scored:
        first_eval_pos = min(pos[id(s)] for s, _ in scored)
        earlier_errors = [s for s in errors if pos[id(s)] < first_eval_pos]
        evals_after = [(s, sc) for s, sc in scored
                       if any(pos[id(e)] < pos[id(s)] for e in earlier_errors)]
        if earlier_errors and evals_after:
            scores = [sc for _, sc in evals_after]
            correlations.append(_correlation(
                "errors_preceded_eval_score",
                [
                    {"type": "error_spans",
                     "span_ids": [s.span_id for s in earlier_errors],
                     "value": len(earlier_errors)},
                    {"type": "eval_scores",
                     "span_ids": [s.span_id for s, _ in evals_after],
                     "value": min(scores)},
                ],
                {
                    "error_spans_before": len(earlier_errors),
                    "eval_scores_after": len(scores),
                    "min_score": min(scores),
                    "max_score": max(scores),
                },
                "errors started before the evaluator span — the eval "
                "score may reflect the failure, measuring the same "
                "event rather than being caused by it",
                f"{len(earlier_errors)} error span(s) preceded "
                f"{len(scores)} evaluator score(s) "
                f"(min {min(scores)})",
                order,
            ))

    correlations.sort(
        key=lambda c: (c["kind"], c["evidence_span_ids"] or [""])
    )
    return {
        "trace_id": trace.trace_id,
        "correlations": correlations,
        "signal_inventory": {
            "spans": len(spans),
            "tool_spans": len(tools),
            "tool_error_spans": sum(1 for s in tools if _is_error(s)),
            "llm_spans": len(llms),
            "llm_with_usage": sum(
                1 for s in llms if extract_usage(s) is not None
            ),
            "llm_with_model": sum(
                1 for s in llms if _model_of(s) is not None
            ),
            "evaluator_spans": sum(
                1 for s in spans if s.kind is SpanKind.EVALUATOR
            ),
            "eval_scores_observed": len(scored),
            "error_spans": len(errors),
            "models": sorted(by_model),
            "tools": sorted(tool_by_name),
        },
        "classification": TEMPORAL,
        "note": "intra-trace associations pair signals by temporal "
                "precedence only — correlation is not causation",
    }


# -- fleet ---------------------------------------------------------------


def _trace_features(trace: Trace) -> dict[str, Any]:
    """Flat signal row for one trace — the evidence behind every
    fleet table. Counts only; no payloads."""
    spans = _ordered(trace)
    counts: Counter[str] = Counter()
    tool_errs: set[str] = set()
    tools: set[str] = set()
    models: set[str] = set()
    llm_calls = llm_with_usage = 0
    declared = 0
    for s in spans:
        if s.kind in (SpanKind.TOOL, SpanKind.LLM):
            name = _semantic_name(s)
            counts[name] += 1
            declared += _declared_retries(s)
        if s.kind is SpanKind.TOOL:
            tools.add(_semantic_name(s))
            if _is_error(s):
                tool_errs.add(_semantic_name(s))
        if s.kind is SpanKind.LLM:
            llm_calls += 1
            if extract_usage(s) is not None:
                llm_with_usage += 1
            m = _model_of(s)
            if m is not None:
                models.add(m)
    scores = [sc for _, sc in _eval_scores(trace)]
    repeated = sum(c - 1 for c in counts.values() if c > 1)
    return {
        "trace_id": trace.trace_id,
        "session_id": trace.session_id,
        "outcome": trace.status.code.value,
        "span_count": len(spans),
        "tools": sorted(tools),
        "tools_errored": sorted(tool_errs),
        "models": sorted(models),
        "llm_calls": llm_calls,
        "llm_with_usage": llm_with_usage,
        "repeated_invocations": repeated,
        "declared_retry_attrs": declared,
        "retry_signals": repeated + declared,
        "eval_scores": scores,
        "eval_score_mean": (
            round(sum(scores) / len(scores), 6) if scores else None
        ),
        "session_traces_in_window": None,  # filled by correlate_fleet
    }


def _presence_table(
    features: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    ok: list[dict[str, Any]],
    other: list[dict[str, Any]],
    field: str,
    key_name: str,
    errored_field: str | None = None,
) -> dict[str, Any]:
    """Per-key presence rates: key in N% of failed vs M% of ok traces.

    Rates are descriptive co-occurrence — a key present only in failed
    traces is the interesting case and is kept, with n disclosed.
    ``errored_field`` (tools only) additionally counts traces where
    the key itself errored.
    """
    keys = sorted({k for f in features for k in f[field]})
    cohort_ok = len(failed) >= _MIN_COHORT and len(ok) >= _MIN_COHORT
    rows: list[dict[str, Any]] = []
    for k in keys:
        in_failed = sum(1 for f in failed if k in f[field])
        in_ok = sum(1 for f in ok if k in f[field])
        in_other = sum(1 for f in other if k in f[field])
        rate_f = in_failed / len(failed) if failed else None
        rate_o = in_ok / len(ok) if ok else None
        diff = (
            round((rate_f - rate_o) * 100, 2)
            if rate_f is not None and rate_o is not None
            else None
        )
        n_block: dict[str, int] = {
            "failed_with": in_failed,
            "failed_total": len(failed),
            "ok_with": in_ok,
            "ok_total": len(ok),
            "other_with": in_other,
            "other_total": len(other),
        }
        if errored_field is not None:
            n_block["failed_with_error"] = sum(
                1 for f in failed if k in f[errored_field]
            )
            n_block["ok_with_error"] = sum(
                1 for f in ok if k in f[errored_field]
            )
        sufficient = cohort_ok
        rows.append({
            key_name: k,
            "n": n_block,
            "rate_failed": round(rate_f, 6) if rate_f is not None else None,
            "rate_ok": round(rate_o, 6) if rate_o is not None else None,
            "rate_difference_pp": diff,
            "sufficient": sufficient,
            "classification": (
                CO_OCCURRENCE if sufficient else INSUFFICIENT
            ),
            **({} if sufficient else {
                "reason": (
                    f"cohorts too small — need ≥{_MIN_COHORT} failed "
                    f"and ≥{_MIN_COHORT} ok traces in the window"
                )
            }),
        })
    rows.sort(key=lambda r: (
        r["rate_difference_pp"] is None,
        -(r["rate_difference_pp"] or 0.0),
        r[key_name],
    ))
    return {
        "basis": f"trace carries ≥1 span labelled by {field}",
        "cohorts": {
            "failed": len(failed), "ok": len(ok), "other": len(other),
        },
        "rows": rows,
        "classification": (
            CO_OCCURRENCE if cohort_ok else INSUFFICIENT
        ),
        "note": "presence rates per outcome class — descriptive "
                "co-occurrence, not a tested difference",
    }


def _session_length_table(
    features: list[dict[str, Any]],
    n_total: int,
    n_failed: int,
) -> dict[str, Any]:
    """Failure rate by session size observed *within the window*."""
    window_rate = n_failed / n_total if n_total else None
    scoped = [f for f in features if f["session_id"] is not None]

    def _bucket(n: int) -> str:
        if n <= 1:
            return "1"
        if n <= 4:
            return "2-4"
        return "5+"

    rows: list[dict[str, Any]] = []
    for label in ("1", "2-4", "5+"):
        members = [
            f for f in scoped
            if _bucket(f["session_traces_in_window"] or 0) == label
        ]
        failed_n = sum(1 for f in members if f["outcome"] == "error")
        n = len(members)
        rate = failed_n / n if n else None
        delta = (
            round((rate - window_rate) * 100, 2)
            if rate is not None and window_rate is not None
            else None
        )
        sufficient = n >= _MIN_COHORT
        rows.append({
            "bucket": label,
            "traces": n,
            "failed": failed_n,
            "failure_rate": round(rate, 6) if rate is not None else None,
            "rate_delta_vs_window_pp": delta,
            "sufficient": sufficient,
            "classification": CO_OCCURRENCE if sufficient else INSUFFICIENT,
            **({} if sufficient else {
                "reason": f"fewer than {_MIN_COHORT} traces in bucket"
            }),
        })
    return {
        "basis": "session size = traces sharing session_id inside the "
                 "bounded window (not the fleet-wide session total)",
        "window_failure_rate": (
            round(window_rate, 6) if window_rate is not None else None
        ),
        "traces_without_session": n_total - len(scoped),
        "rows": rows,
        "note": "session-length is a within-window count — a long "
                "session clipped by the bound reads shorter",
    }


def _retry_eval_table(features: list[dict[str, Any]]) -> dict[str, Any]:
    """Observed eval-score means bucketed by retry-signal count."""
    all_means = [
        f["eval_score_mean"] for f in features
        if f["eval_score_mean"] is not None
    ]
    window_mean = (
        sum(all_means) / len(all_means) if all_means else None
    )

    def _bucket(n: int) -> str:
        if n <= 0:
            return "0"
        if n <= 2:
            return "1-2"
        return "3+"

    rows: list[dict[str, Any]] = []
    for label in ("0", "1-2", "3+"):
        members = [f for f in features if _bucket(f["retry_signals"]) == label]
        means = [
            f["eval_score_mean"] for f in members
            if f["eval_score_mean"] is not None
        ]
        mean = sum(means) / len(means) if means else None
        delta = (
            round(mean - window_mean, 6)
            if mean is not None and window_mean is not None
            else None
        )
        sufficient = len(means) >= _MIN_SCORED
        rows.append({
            "bucket": label,
            "traces": len(members),
            "scored_traces": len(means),
            "mean_score": round(mean, 6) if mean is not None else None,
            "score_delta_vs_window": delta,
            "sufficient": sufficient,
            "classification": CO_OCCURRENCE if sufficient else INSUFFICIENT,
            **({} if sufficient else {
                "reason": (
                    f"fewer than {_MIN_SCORED} scored traces in bucket"
                )
            }),
        })
    return {
        "basis": "retry_signals = repeated same-name invocations + "
                 "declared retry counters; scores = numeric attributes "
                 "observed on EVALUATOR-kind spans",
        "window_mean_score": (
            round(window_mean, 6) if window_mean is not None else None
        ),
        "traces_scored": len(all_means),
        "traces_total": len(features),
        "rows": rows,
        "note": "repeated invocations are a retry proxy, not declared "
                "retries — and score co-occurrence is not evidence "
                "that retries moved the score",
    }


def correlate_fleet(
    store: TraceStore,
    project_id: str | None,
    *,
    limit: int = 200,
) -> dict[str, Any]:
    """Co-occurrence tables over a bounded trace window.

    The window is the ≤``limit`` most recent traces (hard-capped at
    500, same discipline as ``/v1/ops/summary``). Every table discloses
    its n per cohort; cohorts smaller than the floor are labelled
    ``insufficient_data`` — the numbers are still shown, never
    smoothed, and no row claims causation or significance.
    """
    applied = max(0, min(int(limit), _MAX_WINDOW))
    rows = store.list_traces(project_id=project_id, limit=applied)
    traces: list[Trace] = []
    for r in rows:
        t = store.get_trace(r["trace_id"], project_id=r["project_id"])
        if t is not None:
            traces.append(t)

    features = [_trace_features(t) for t in traces]
    session_counts: Counter[str] = Counter(
        f["session_id"] for f in features if f["session_id"] is not None
    )
    for f in features:
        if f["session_id"] is not None:
            f["session_traces_in_window"] = session_counts[f["session_id"]]

    failed = [f for f in features if f["outcome"] == "error"]
    ok = [f for f in features if f["outcome"] == "ok"]
    other = [
        f for f in features
        if f["outcome"] not in ("error", "ok")
    ]
    features.sort(key=lambda f: f["trace_id"])

    return {
        "project_id": project_id,
        "window": {
            "limit_requested": limit,
            "limit_applied": applied,
            "traces": len(features),
        },
        "outcomes": {
            "failed": len(failed),
            "ok": len(ok),
            "other": len(other),
        },
        "tables": {
            "tool_error": _presence_table(
                features, failed, ok, other, "tools", "tool",
                errored_field="tools_errored",
            ),
            "model_error": _presence_table(
                features, failed, ok, other, "models", "model"
            ),
            "session_length_error": _session_length_table(
                features, len(features), len(failed)
            ),
            "retry_eval": _retry_eval_table(features),
        },
        "trace_signals": features,
        "classification": "descriptive_co_occurrence",
        "note": "co-occurrence over a bounded window — descriptive "
                "rates only, no causal claim and no significance "
                "test; use /v1/eval/compare for tested comparisons",
    }
