"""Evaluation-scenario suggestions from failure evidence (Cycle 12 §12.7).

Recurring failure categories map to *candidate* evaluator specs and
dataset-case sketches — e.g. repeated ``invalid_arguments`` on a tool
suggests an ``allowed_tools`` guard plus dataset cases replaying the
rejected call. Suggestions are emitted with their evidence and marked
``classification: candidate``: they are recommendations to review, never
auto-applied — datasets are never modified by this engine.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from forge_lens.investigate.classify import (
    RETRY_EXHAUSTION_THRESHOLD,
    FailureCategory,
    FailureReport,
)

# Per-category templates. ``params`` carries values derivable from the
# reports themselves; ``params_required`` names keys the reviewer must
# fill (SLOs live outside the traces). ``tools_param`` marks the param
# that receives the observed failing-span names.
_TEMPLATES: dict[FailureCategory, tuple[dict[str, Any], ...]] = {
    FailureCategory.INVALID_ARGUMENTS: (
        {
            "kind": "evaluator_spec",
            "name": "allowed_tools",
            "params": {},
            "tools_param": "allowed",
            "rationale": (
                "invalid-argument failures on these tools — pinning the "
                "allowed tool surface makes off-schema calls fail in "
                "eval before production"
            ),
        },
        {
            "kind": "dataset_case",
            "name": "invalid_args_rejected",
            "params": {},
            "rationale": (
                "replay the rejected call shapes as dataset cases — "
                "assert the agent corrects or declines bad args instead "
                "of erroring"
            ),
        },
    ),
    FailureCategory.SCHEMA_MISMATCH: (
        {
            "kind": "evaluator_spec",
            "name": "regex",
            "params": {},
            "params_required": ("pattern",),
            "rationale": (
                "output failed schema/parse — a shape guard on the "
                "output field catches non-conforming responses early"
            ),
        },
        {
            "kind": "dataset_case",
            "name": "schema_conformance",
            "params": {},
            "rationale": (
                "add cases asserting the response parses to the declared "
                "output schema — covers model/version drift"
            ),
        },
    ),
    FailureCategory.AUTH_ERROR: (
        {
            "kind": "dataset_case",
            "name": "expired_or_invalid_credential",
            "params": {},
            "rationale": (
                "replay the auth-failure scenario — assert the agent "
                "surfaces the credential problem instead of retrying "
                "blind"
            ),
        },
        {
            "kind": "evaluator_spec",
            "name": "error_free",
            "params": {},
            "rationale": "baseline — auth failures must not recur",
        },
    ),
    FailureCategory.MISSING_PERMISSIONS: (
        {
            "kind": "dataset_case",
            "name": "insufficient_scope",
            "params": {},
            "rationale": (
                "replay the 403 scenario — assert the agent degrades or "
                "reports the missing permission explicitly"
            ),
        },
        {
            "kind": "evaluator_spec",
            "name": "error_free",
            "params": {},
            "rationale": "baseline — permission failures must not recur",
        },
    ),
    FailureCategory.RETRY_EXHAUSTION: (
        {
            "kind": "evaluator_spec",
            "name": "tool_call_count",
            "params": {"min": 0, "max": RETRY_EXHAUSTION_THRESHOLD},
            "rationale": (
                "retry exhaustion implies a retry storm — bounding "
                "tool-call volume fails runs that loop past the cap"
            ),
        },
        {
            "kind": "evaluator_spec",
            "name": "error_free",
            "params": {},
            "rationale": "baseline — exhausted retries must not recur",
        },
    ),
    FailureCategory.AGENT_CANCELLED: (
        {
            "kind": "evaluator_spec",
            "name": "task_completion",
            "params": {},
            "rationale": (
                "cancelled runs never complete — task_completion fails "
                "them directly"
            ),
        },
        {
            "kind": "evaluator_spec",
            "name": "latency_budget",
            "params": {},
            "params_required": ("max_duration_ms",),
            "rationale": (
                "if cancellation followed a deadline, a latency budget "
                "codifies the limit the run missed"
            ),
        },
    ),
    FailureCategory.BUDGET_EXCEEDED: (
        {
            "kind": "evaluator_spec",
            "name": "token_budget",
            "params": {},
            "params_required": ("max_total_tokens",),
            "rationale": (
                "budget exhaustion — a token budget eval makes the limit "
                "an assertion instead of a surprise"
            ),
        },
        {
            "kind": "dataset_case",
            "name": "budget_pressure",
            "params": {},
            "rationale": (
                "cases near the budget boundary — assert the agent "
                "shrinks context instead of blowing the limit"
            ),
        },
    ),
    FailureCategory.RATE_LIMIT: (
        {
            "kind": "dataset_case",
            "name": "provider_throttle",
            "params": {},
            "rationale": (
                "replay a 429 — assert backoff/degrade behavior instead "
                "of hard failure"
            ),
        },
        {
            "kind": "evaluator_spec",
            "name": "error_free",
            "params": {},
            "rationale": "baseline — throttled failures must not recur",
        },
    ),
    FailureCategory.TIMEOUT: (
        {
            "kind": "evaluator_spec",
            "name": "latency_budget",
            "params": {},
            "params_required": ("max_duration_ms",),
            "rationale": (
                "timeout failure — a latency budget codifies the "
                "deadline as an eval gate"
            ),
        },
        {
            "kind": "evaluator_spec",
            "name": "task_completion",
            "params": {},
            "rationale": "timed-out runs must still be counted complete "
            "or failed — never silent",
        },
    ),
    FailureCategory.TOOL_ERROR: (
        {
            "kind": "evaluator_spec",
            "name": "tool_success",
            "params": {},
            "rationale": "tool errors — tool_success tracks the failing "
            "fraction directly",
        },
        {
            "kind": "evaluator_spec",
            "name": "allowed_tools",
            "params": {},
            "tools_param": "allowed",
            "rationale": (
                "if the failing tool is not essential, restricting the "
                "allowed surface removes the failure mode"
            ),
        },
    ),
    FailureCategory.LLM_ERROR: (
        {
            "kind": "evaluator_spec",
            "name": "error_free",
            "params": {},
            "rationale": "baseline — provider errors must not recur",
        },
        {
            "kind": "evaluator_spec",
            "name": "token_budget",
            "params": {},
            "params_required": ("max_total_tokens",),
            "rationale": (
                "when the error is context-size driven, a token budget "
                "fails oversized prompts"
            ),
        },
    ),
    FailureCategory.GUARDRAIL: (
        {
            "kind": "evaluator_spec",
            "name": "allowed_tools",
            "params": {},
            "tools_param": "allowed",
            "rationale": (
                "guardrail-blocked calls — the allowed surface should "
                "encode what the guardrail rejected"
            ),
        },
    ),
    FailureCategory.INCOMPLETE: (
        {
            "kind": "evaluator_spec",
            "name": "task_completion",
            "params": {},
            "rationale": (
                "truncated runs never end — task_completion fails them "
                "directly"
            ),
        },
        {
            "kind": "evaluator_spec",
            "name": "latency_budget",
            "params": {},
            "params_required": ("max_duration_ms",),
            "rationale": (
                "if truncation was a host deadline, a latency budget "
                "codifies it"
            ),
        },
    ),
    FailureCategory.CYCLE: (
        {
            "kind": "dataset_case",
            "name": "parentage_integrity",
            "params": {},
            "rationale": (
                "corrupted parent pointers — a dataset case with "
                "expected graph shape guards the producer fix"
            ),
        },
    ),
    # UNKNOWN: no suggestion — recommending an eval for an unclassified
    # failure would be inventing the cause we refused to invent.
}


def suggest_evaluators(
    reports: Iterable[FailureReport],
) -> dict[str, Any]:
    """Suggest evaluator specs / dataset cases from failure reports.

    Accepts one report or many — pass ``classify_failure(t)`` for a
    single trace, or a list across stored traces for the recurring-
    failure view (``recurring`` marks categories seen in ≥2 distinct
    traces). Suggestions are ``candidate``-classified: review and apply
    by hand — nothing here mutates a dataset.
    """
    cat_traces: dict[FailureCategory, set[str]] = {}
    cat_names: dict[FailureCategory, set[str]] = {}
    for r in reports:
        if not r.failed or r.category is FailureCategory.UNKNOWN:
            continue
        cat_traces.setdefault(r.category, set()).add(r.trace_id)
        names = r.evidence.get("names") or []
        for n in names:
            cat_names.setdefault(r.category, set()).add(str(n))

    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for cat in sorted(cat_traces, key=lambda c: c.value):
        traces = sorted(cat_traces[cat])
        tools = sorted(cat_names.get(cat, set()))
        for tmpl in _TEMPLATES.get(cat, ()):
            params = dict(tmpl.get("params") or {})
            tools_param = tmpl.get("tools_param")
            if tools_param:
                params[tools_param] = tools
            evidence = {
                "category": cat.value,
                "traces": traces,
                "count": len(traces),
                "recurring": len(traces) >= 2,
            }
            key = (
                str(tmpl["kind"]),
                str(tmpl["name"]),
                json.dumps(params, sort_keys=True),
            )
            if key in merged:
                prev = merged[key]
                prev["evidence"]["traces"] = sorted(
                    set(prev["evidence"]["traces"]) | set(traces)
                )
                prev["evidence"]["count"] = len(prev["evidence"]["traces"])
                prev["evidence"]["recurring"] = (
                    prev["evidence"]["count"] >= 2
                )
                if cat.value not in prev["evidence"]["categories"]:
                    prev["evidence"]["categories"].append(cat.value)
                    prev["evidence"]["categories"].sort()
                continue
            merged[key] = {
                "kind": tmpl["kind"],
                "name": tmpl["name"],
                "params": params,
                "params_required": list(tmpl.get("params_required") or ()),
                "rationale": tmpl["rationale"],
                "evidence": {**evidence, "categories": [cat.value]},
                "classification": "candidate",
            }

    suggestions = sorted(
        merged.values(), key=lambda s: (s["kind"], s["name"])
    )
    return {
        "suggestions": suggestions,
        "note": (
            "candidate suggestions from failure evidence — apply "
            "manually; datasets are never auto-modified"
        ),
    }


__all__ = ["suggest_evaluators"]
