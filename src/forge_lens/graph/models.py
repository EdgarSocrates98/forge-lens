"""Graph view models — JSON-serializable, deterministic, evidence-based."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from forge_lens.contracts.base import LensModel


class EdgeKind(StrEnum):
    """How two nodes relate."""

    PARENT = "parent"
    """Tree containment: ``parent_span_id`` observed on the child."""

    SEQUENCE = "sequence"
    """Temporal succession between siblings ordered by ``start_time``."""

    LINK = "link"
    """Explicit ``SpanLink`` — may point outside this graph's node set."""


class GraphNode(LensModel):
    """A span (or trace, for session graphs) placed on the graph."""

    id: str
    kind: str
    """Span kind value (``llm``/``tool``/…) or ``trace`` in session graphs."""
    name: str
    start_time: datetime
    end_time: datetime | None = None
    duration_ms: float | None = None
    status: str = "unset"
    integrity_flags: list[str] = Field(default_factory=list)
    has_token_usage: bool = False
    error: bool = False


class GraphEdge(LensModel):
    source: str
    target: str
    kind: EdgeKind
    dangling: bool = False
    """True when the target id is not a node in this graph (cross-trace
    link or a parent that never arrived — evidence, not an error)."""


class ExecutionGraph(LensModel):
    """Deterministic node/edge projection of a trace or session."""

    subject_id: str
    """The trace_id (or session_id) this graph was built for."""
    project_id: str
    root_id: str | None = None
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    """``span_count``, ``edge_count``, ``max_depth``, ``dangling_edges``…"""
