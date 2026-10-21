"""Trace and span contracts (v1).

The shape mirrors OpenTelemetry closely enough to be a lossless landing
zone for OTLP/HTTP JSON, while remaining flat enough for the local store
and the Studio UI.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.provenance import Provenance


class StatusCode(StrEnum):
    UNSET = "unset"
    OK = "ok"
    ERROR = "error"


class Status(LensModel):
    code: StatusCode = StatusCode.UNSET
    message: str | None = None


class SpanKind(StrEnum):
    """Unified span kind.

    OpenInference conceptual kinds (AGENT, CHAIN, LLM, TOOL, RETRIEVER,
    EMBEDDING, RERANKER) coexist with the OpenTelemetry wire kinds
    (INTERNAL, SERVER, CLIENT, PRODUCER, CONSUMER). ``OTHER`` is the
    honest fallback — never guess a kind we did not observe.
    """

    INTERNAL = "internal"
    SERVER = "server"
    CLIENT = "client"
    PRODUCER = "producer"
    CONSUMER = "consumer"
    AGENT = "agent"
    CHAIN = "chain"
    LLM = "llm"
    TOOL = "tool"
    RETRIEVER = "retriever"
    EMBEDDING = "embedding"
    RERANKER = "reranker"
    GUARDRAIL = "guardrail"
    EVALUATOR = "evaluator"
    OTHER = "other"


class IntegrityFlag(StrEnum):
    """Data-quality markers computed at ingestion or query time."""

    MISSING_PARENT = "missing_parent"
    ORPHAN = "orphan"
    OUT_OF_ORDER = "out_of_order"
    DUPLICATE = "duplicate"
    PARTIAL_TRACE = "partial_trace"
    CLOCK_SKEW = "clock_skew"
    CONFLICT = "conflict"
    TRUNCATED = "truncated"
    UNKNOWN_TOKENS = "unknown_tokens"
    MISSING_END = "missing_end"


class SpanEvent(LensModel):
    name: str
    time: datetime
    attributes: dict[str, Any] = Field(default_factory=dict)


class SpanLink(LensModel):
    trace_id: str
    span_id: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class Span(LensModel):
    """A single unit of work inside a trace."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    kind: SpanKind = SpanKind.INTERNAL
    start_time: datetime
    end_time: datetime | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    events: list[SpanEvent] = Field(default_factory=list)
    links: list[SpanLink] = Field(default_factory=list)
    status: Status = Status()
    provenance: Provenance = Provenance.OBSERVED
    integrity_flags: list[IntegrityFlag] = Field(default_factory=list)
    """Non-fatal data-quality observations attached at ingestion."""

    @property
    def duration_ms(self) -> float | None:
        if self.end_time is None:
            return None
        return (self.end_time - self.start_time).total_seconds() * 1000.0


class TraceSource(LensModel):
    """Where a trace was produced."""

    system: str
    """Producer system: ``forge-lens-sdk``, ``otlp``, ``claude-code``, ``mcp``…"""

    sdk_name: str | None = None
    sdk_version: str | None = None
    host: str | None = None
    runtime: str | None = None
    """E.g. ``python-3.12``, ``node-22``, ``unknown``."""

    language: str | None = None


class Trace(LensModel):
    """A complete (or partial) execution tree."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    project_id: str = "default"
    session_id: str | None = None
    root_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    started_at: datetime
    ended_at: datetime | None = None
    status: Status = Status()
    source: TraceSource = TraceSource(system="unknown")
    resource_attributes: dict[str, Any] = Field(default_factory=dict)
    spans: list[Span] = Field(default_factory=list)
    provenance: Provenance = Provenance.OBSERVED


class TraceIngest(LensModel):
    """Native ingestion envelope for ``POST /v1/traces``."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    project_id: str = "default"
    source: TraceSource | None = None
    traces: list[Trace]
    sent_at: datetime | None = None
