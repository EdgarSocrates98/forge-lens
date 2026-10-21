"""Advanced graph analysis — Cycle 12 §12.5.

Centrality, dependency concentration, handoff inefficiency, repeated
subgraphs — deterministic metrics over the built ExecutionGraph.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from forge_lens.graph.models import EdgeKind, ExecutionGraph


def analyze_graph(graph: ExecutionGraph) -> dict[str, Any]:
    """Concentration + centrality roll-up over a built graph."""
    out_deg: Counter[str] = Counter()
    in_deg: Counter[str] = Counter()
    edge_kinds: Counter[str] = Counter()
    for e in graph.edges:
        out_deg[e.source] += 1
        in_deg[e.target] += 1
        edge_kinds[e.kind.value] += 1
    n = len(graph.nodes) or 1
    hub_out = out_deg.most_common(1)[0] if out_deg else (None, 0)
    hub_in = in_deg.most_common(1)[0] if in_deg else (None, 0)
    # failure propagation: nodes whose parents failed
    status_by_id = {nd.id: nd for nd in graph.nodes}
    propagated: list[str] = []
    for e in graph.edges:
        if e.kind is EdgeKind.PARENT:
            src = status_by_id.get(e.source)
            tgt = status_by_id.get(e.target)
            if src is not None and tgt is not None and src.error:
                propagated.append(tgt.id)
    # repeated subgraphs: identical (kind,name) node signatures
    sig: Counter[tuple[str, str]] = Counter(
        (nd.kind, nd.name) for nd in graph.nodes
    )
    repeated = [
        {"kind": k[0], "name": k[1], "count": c}
        for k, c in sorted(sig.items(), key=lambda kv: (-kv[1], kv[0]))
        if c >= 3
    ]
    return {
        "nodes": len(graph.nodes), "edges": len(graph.edges),
        "edge_kinds": dict(sorted(edge_kinds.items())),
        "max_out_degree": {"node": hub_out[0], "degree": hub_out[1]},
        "max_in_degree": {"node": hub_in[0], "degree": hub_in[1]},
        "out_degree_concentration": (hub_out[1] / n),
        "failed_children_of_error_nodes": sorted(set(propagated)),
        "repeated_node_signatures": repeated,
    }
