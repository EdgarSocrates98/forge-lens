"""Deterministic analytics over :class:`ExecutionGraph`.

All functions are pure — they take the built graph, return evidence.
"""

from __future__ import annotations

from forge_lens.graph.models import EdgeKind, ExecutionGraph

WHITE, GRAY, BLACK = 0, 1, 2


def _children(graph: ExecutionGraph) -> dict[str, list[str]]:
    """parent id → ordered child ids (parent edges only)."""
    kids: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.kind is EdgeKind.PARENT:
            kids.setdefault(e.source, []).append(e.target)
    node_order = {n.id: n.start_time for n in graph.nodes}
    for group in kids.values():
        # uniform str key — a dangling target (or None start_time) falls
        # back to the id itself, and str() keeps datetime and str from
        # ever meeting in a comparison on hand-built graphs
        group.sort(key=lambda i: (str(node_order.get(i) or i), i))
    return kids


def critical_path(graph: ExecutionGraph) -> dict[str, object]:
    """Root→leaf chain maximizing the sum of node durations.

    In waterfall terms this is the serialized work that dominates the
    trace's wall-clock time. Iterative over an explicit stack — parent
    cycles and arbitrarily deep chains cannot overflow the recursion
    limit. A cycle edge is *cut* deterministically (the edge into an
    in-progress node contributes nothing to the path) and reported in
    ``cycles`` as evidence. Multi-root partial traces evaluate every
    root candidate and take the maximum — ties break on the
    lexicographically smallest path.
    """
    durations = {n.id: max(0.0, n.duration_ms or 0.0) for n in graph.nodes}
    kids = _children(graph)
    color = {n.id: WHITE for n in graph.nodes}
    memo: dict[str, tuple[float, list[str]]] = {}
    cycles: list[dict[str, str]] = []

    def best(entry: str) -> None:
        # iterative post-order: each stack entry is (node, expanded?)
        stack: list[tuple[str, bool]] = [(entry, False)]
        while stack:
            node, expanded = stack.pop()
            if not expanded:
                # nodes are pushed only while WHITE, so a (node, False)
                # entry can only pop as WHITE or BLACK (already resolved
                # from another entry point)
                if color[node] == BLACK:
                    continue
                color[node] = GRAY
                stack.append((node, True))
                for child in sorted(kids.get(node, []), reverse=True):
                    if color[child] == GRAY:
                        # edge closes a cycle — cut it for path purposes,
                        # keep it as evidence in `cycles`
                        cycles.append({"from": node, "to": child})
                        continue
                    if color[child] == WHITE:
                        stack.append((child, False))
            else:
                color[node] = BLACK
                best_child: tuple[float, list[str]] = (0.0, [])
                for child in sorted(kids.get(node, [])):
                    cand = memo.get(child)
                    if cand is None:
                        continue  # child is in a cycle cut — contributes 0
                    if cand[0] > best_child[0] or (
                        cand[0] == best_child[0]
                        and cand[1]
                        and cand[1] < (best_child[1] or ["~"])
                    ):
                        best_child = cand
                memo[node] = (
                    durations.get(node, 0.0) + best_child[0],
                    [node, *best_child[1]],
                )

    # evaluate every node — covers multi-root partial traces and
    # components that contain the cycle cut
    for node_id in sorted(durations):
        best(node_id)

    best_total = 0.0
    best_path: list[str] = []
    for node_id in sorted(durations):
        w, path = memo.get(node_id, (0.0, []))
        if w > best_total or (w == best_total and path and path < (best_path or ["~"])):
            best_total, best_path = w, path
    return {
        "path": best_path,
        "total_ms": round(best_total, 3),
        "cycles": cycles,
    }


def self_time_ranking(graph: ExecutionGraph, top: int = 5) -> list[dict[str, object]]:
    """Bottleneck ranking: node duration minus direct-children duration.

    Self-time is where the trace actually *spends* time — a parent whose
    children cover its whole duration adds orchestration, not work.
    Children may overlap, so self-time is clamped at 0 (floor, not
    invented negative work).
    """
    kids = _children(graph)
    durations = {n.id: max(0.0, n.duration_ms or 0.0) for n in graph.nodes}
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
