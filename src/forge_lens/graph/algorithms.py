"""Deterministic analytics over :class:`ExecutionGraph`.

All functions are pure — they take the built graph, return evidence.
"""

from __future__ import annotations

from forge_lens.graph.models import EdgeKind, ExecutionGraph


def _children(graph: ExecutionGraph) -> dict[str, list[str]]:
    """parent id → ordered child ids (parent edges only)."""
    kids: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.kind is EdgeKind.PARENT:
            kids.setdefault(e.source, []).append(e.target)
    node_order = {n.id: n.start_time for n in graph.nodes}
    for group in kids.values():
        group.sort(key=lambda i: (node_order.get(i) or i, i))
    return kids


def critical_path(graph: ExecutionGraph) -> dict[str, object]:
    """Root→leaf chain maximizing the sum of node durations.

    In waterfall terms this is the serialized work that dominates the
    trace's wall-clock time. Ties break on lexicographic node id —
    deterministic, never random.
    """
    durations = {n.id: (n.duration_ms or 0.0) for n in graph.nodes}
    kids = _children(graph)
    memo: dict[str, tuple[float, list[str]]] = {}

    def best(node_id: str) -> tuple[float, list[str]]:
        if node_id in memo:
            return memo[node_id]
        best_child: tuple[float, list[str]] = (0.0, [])
        for child in sorted(kids.get(node_id, [])):
            cand = best(child)
            if cand[0] > best_child[0] or (
                cand[0] == best_child[0] and cand[1] and cand[1] < (best_child[1] or ["~"])
            ):
                best_child = cand
        memo[node_id] = (
            durations.get(node_id, 0.0) + best_child[0],
            [node_id, *best_child[1]],
        )
        return memo[node_id]

    if graph.root_id is None or graph.root_id not in durations:
        return {"path": [], "total_ms": 0.0}
    weight, path = best(graph.root_id)
    return {"path": path, "total_ms": round(weight, 3)}


def self_time_ranking(graph: ExecutionGraph, top: int = 5) -> list[dict[str, object]]:
    """Bottleneck ranking: node duration minus direct-children duration.

    Self-time is where the trace actually *spends* time — a parent whose
    children cover its whole duration adds orchestration, not work.
    Children may overlap, so self-time is clamped at 0 (floor, not
    invented negative work).
    """
    kids = _children(graph)
    durations = {n.id: (n.duration_ms or 0.0) for n in graph.nodes}
    scored: list[tuple[float, str, dict[str, object]]] = []
    for n in graph.nodes:
        child_ms = sum(durations.get(c, 0.0) for c in kids.get(n.id, []))
        self_ms = round(max(0.0, (n.duration_ms or 0.0) - child_ms), 3)
        scored.append(
            (
                self_ms,
                n.id,
                {
                    "id": n.id,
                    "name": n.name,
                    "kind": n.kind,
                    "self_ms": self_ms,
                    "duration_ms": n.duration_ms,
                },
            )
        )
    scored.sort(key=lambda r: (-r[0], r[1]))
    return [row for _, _, row in scored[: max(0, top)]]
