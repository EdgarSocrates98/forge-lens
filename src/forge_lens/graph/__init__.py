"""Temporal execution-graph intelligence (Cycle 03).

Builds a node/edge view over stored spans: tree (``parent``), temporal
succession among siblings (``sequence``), and cross-trace references
(``link``). Pure functions over contracts — no I/O, no wall-clock.
"""

from forge_lens.graph.algorithms import (
    critical_path,
    self_time_ranking,
)
from forge_lens.graph.builder import build_session_graph, build_trace_graph
from forge_lens.graph.models import EdgeKind, ExecutionGraph, GraphEdge, GraphNode

__all__ = [
    "EdgeKind",
    "ExecutionGraph",
    "GraphEdge",
    "GraphNode",
    "build_session_graph",
    "build_trace_graph",
    "critical_path",
    "self_time_ranking",
]
