"""Build :class:`ExecutionGraph` projections from stored traces.

Determinism: nodes are emitted sorted by ``(start_time, span_id)`` and
edges by ``(kind, source, target)`` — same input, same graph.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable

from forge_lens.contracts.v1.trace import Span, Trace
from forge_lens.graph.models import EdgeKind, ExecutionGraph, GraphEdge, GraphNode

WHITE, GRAY, BLACK = 0, 1, 2


def _node(span: Span) -> GraphNode:
    from forge_lens.intel.cost import extract_usage

    return GraphNode(
        id=span.span_id,
        kind=span.kind.value,
        name=span.name,
        start_time=span.start_time,
        end_time=span.end_time,
        duration_ms=span.duration_ms,
        status=span.status.code.value,
        integrity_flags=[f.value for f in span.integrity_flags],
        # a usage dict that doesn't parse is not usage — same bar the
        # UNKNOWN_TOKENS flag applies
        has_token_usage=extract_usage(span) is not None,
        error=span.status.code.value == "error",
    )


def build_trace_graph(trace: Trace) -> ExecutionGraph:
    """Project one trace's spans into parent/sequence/link edges.

    - ``parent``: child → its observed ``parent_span_id`` when that parent
      is present in the node set; absent parents stay evidence (the
      ``missing_parent`` flag on the node) — no invented nodes.
    - ``sequence``: siblings (same parent, including root level) ordered
      by ``start_time`` chain to the next sibling.
    - ``link``: observed ``SpanLink`` targets; targets outside the node
      set are marked ``dangling`` rather than dropped.
    """
    spans = sorted(trace.spans, key=lambda s: (s.start_time, s.span_id))
    ids = {s.span_id for s in spans}
    nodes = [_node(s) for s in spans]
    edges: list[GraphEdge] = []

    # siblings ordered per parent → temporal succession chain
    siblings: dict[str | None, list[Span]] = {}
    for s in spans:
        parent = s.parent_span_id if s.parent_span_id in ids else None
        siblings.setdefault(parent, []).append(s)
        if s.parent_span_id and s.parent_span_id in ids:
            edges.append(
                GraphEdge(
                    source=s.parent_span_id, target=s.span_id, kind=EdgeKind.PARENT
                )
            )
        for link in s.links:
            edges.append(
                GraphEdge(
                    source=s.span_id,
                    target=link.span_id,
                    kind=EdgeKind.LINK,
                    dangling=link.span_id not in ids,
                )
            )

    for group in siblings.values():
        for prev, nxt in itertools.pairwise(group):
            edges.append(
                GraphEdge(source=prev.span_id, target=nxt.span_id, kind=EdgeKind.SEQUENCE)
            )

    edges.sort(key=lambda e: (e.kind.value, e.source, e.target))
    roots = siblings.get(None, [])
    root_id: str | None = (
        trace.root_span_id if trace.root_span_id in ids
        else (roots[0].span_id if roots else None)
    )
    members = cycle_member_ids({s.span_id: s.parent_span_id for s in spans})
    return ExecutionGraph(
        subject_id=trace.trace_id,
        project_id=trace.project_id,
        root_id=root_id,
        nodes=nodes,
        edges=edges,
        stats={
            "span_count": len(nodes),
            "edge_count": len(edges),
            "max_depth": _max_depth(siblings, members),
            "dangling_edges": sum(1 for e in edges if e.dangling),
            "missing_parents": sum(
                1 for s in spans if s.parent_span_id and s.parent_span_id not in ids
            ),
            "cycle_members": len(members),
        },
    )


def cycle_member_ids(parent_of: dict[str, str | None]) -> set[str]:
    """Ids of nodes sitting on a parent-pointer cycle — O(n).

    Parentage is a functional graph (each node has ≤1 parent), so a
    three-color walk finds every cycle member in one pass: a node is a
    member iff walking its ancestor chain re-enters a node still open
    in the *same* walk. Nodes that merely lead into a cycle are visited
    but not members.
    """
    color = dict.fromkeys(parent_of, WHITE)
    members: set[str] = set()
    for start in parent_of:
        if color[start] != WHITE:
            continue
        path: list[str] = []
        node: str | None = start
        while node is not None and node in parent_of and color[node] == WHITE:
            color[node] = GRAY
            path.append(node)
            node = parent_of.get(node)
        if node is not None and node in parent_of and color[node] == GRAY:
            # re-entered a node open in this walk — the cycle is the
            # tail of path from that node on
            members.update(path[path.index(node) :])
        for p in path:
            color[p] = BLACK
    return members


def _max_depth(
    siblings: dict[str | None, list[Span]], cyclic_members: set[str]
) -> int:
    """Longest parent→leaf chain across the WHOLE trace; 0 when empty.

    BFS starts from every root-level span (a partial trace can carry
    several), plus each cycle member as a pseudo-root — trees hanging
    off a cycle still contribute depth. ``seen`` bounds revisits so a
    member's cycle edges terminate rather than loop.
    """
    frontier = [s.span_id for s in siblings.get(None, [])] + sorted(cyclic_members)
    if not frontier:
        return 0
    depth = 0
    seen: set[str] = set()
    while frontier:
        depth += 1
        nxt: list[str] = []
        for node_id in frontier:
            if node_id in seen:
                continue
            seen.add(node_id)
            nxt.extend(s.span_id for s in siblings.get(node_id, []))
        frontier = nxt
    return depth


def build_session_graph(
    session_id: str, project_id: str, traces: Iterable[Trace]
) -> ExecutionGraph:
    """Project a session's traces into a temporal sequence graph.

    Nodes are traces (kind ``trace``); ``sequence`` edges connect
    consecutive executions ordered by ``started_at``.
    """
    ordered = sorted(traces, key=lambda t: (t.started_at, t.trace_id))
    nodes = [
        GraphNode(
            id=t.trace_id,
            kind="trace",
            name=t.root_span_id or t.trace_id[:12],
            start_time=t.started_at,
            end_time=t.ended_at,
            duration_ms=(
                (t.ended_at - t.started_at).total_seconds() * 1000.0
                if t.ended_at is not None
                else None
            ),
            status=t.status.code.value,
            error=t.status.code.value == "error",
        )
        for t in ordered
    ]
    edges = [
        GraphEdge(source=a.trace_id, target=b.trace_id, kind=EdgeKind.SEQUENCE)
        for a, b in itertools.pairwise(ordered)
    ]
    return ExecutionGraph(
        subject_id=session_id,
        project_id=project_id,
        root_id=nodes[0].id if nodes else None,
        nodes=nodes,
        edges=edges,
        stats={
            "trace_count": len(nodes),
            "edge_count": len(edges),
            "dangling_edges": 0,
        },
    )
