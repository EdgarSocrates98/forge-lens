"""Versioned public contracts for Forge Lens.

Contracts are the compatibility boundary of the project. Every model carries
an explicit ``schema_version`` and unknown fields are preserved on a
best-effort basis (``extra="allow"``) so newer producers do not break older
consumers.
"""

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.provenance import Provenance
from forge_lens.contracts.v1 import (
    SCHEMA_VERSION_V1,
    AgentExecution,
    BlobRef,
    CostObservation,
    DataClassification,
    EvaluationResult,
    Evidence,
    SanitizationStatus,
    Span,
    SpanEvent,
    SpanKind,
    SpanLink,
    Status,
    StatusCode,
    TokenUsage,
    ToolInvocation,
    Trace,
    TraceIngest,
    TraceSource,
)

__all__ = [
    "SCHEMA_VERSION_V1",
    "AgentExecution",
    "BlobRef",
    "CostObservation",
    "DataClassification",
    "EvaluationResult",
    "Evidence",
    "LensModel",
    "Provenance",
    "SanitizationStatus",
    "Span",
    "SpanEvent",
    "SpanKind",
    "SpanLink",
    "Status",
    "StatusCode",
    "TokenUsage",
    "ToolInvocation",
    "Trace",
    "TraceIngest",
    "TraceSource",
]
