"""Incident bundle — one call, every evidence artifact for a trace."""

from __future__ import annotations

from typing import Any

from forge_lens.contracts.v1.trace import Trace
from forge_lens.intel.pricing import PricingRegistry


def incident_bundle(trace: Trace, registry: PricingRegistry) -> dict[str, Any]:
    """Assemble the full evidence package for an incident report.

    Everything in the bundle is already derived from stored evidence —
    this composes graph + failure classification + cost + waste +
    timeline into one JSON-serializable handoff.
    """
    from forge_lens.graph import build_trace_graph, critical_path, self_time_ranking
    from forge_lens.intel import detect_waste, trace_cost
    from forge_lens.investigate.classify import classify_failure
    from forge_lens.investigate.eval_suggest import suggest_evaluators
    from forge_lens.investigate.hypothesis import build_hypotheses
    from forge_lens.investigate.replay import replay_timeline

    graph = build_trace_graph(trace)
    report = classify_failure(trace)
    return {
        "trace_id": trace.trace_id,
        "project_id": trace.project_id,
        "session_id": trace.session_id,
        "status": trace.status.code.value,
        "failure": report.model_dump(mode="json"),
        "hypotheses": [
            h.model_dump(mode="json")
            for h in build_hypotheses(trace, report=report)
        ],
        "eval_suggestions": suggest_evaluators([report])["suggestions"],
        "graph": {
            "stats": graph.stats,
            "critical_path": critical_path(graph),
            "bottlenecks": self_time_ranking(graph, top=5),
        },
        "cost": trace_cost(trace, registry).model_dump(mode="json"),
        "waste": [w.model_dump(mode="json") for w in detect_waste(trace, registry)],
        "replay": replay_timeline(trace),
        "bundle_version": "1.0.0",
    }
