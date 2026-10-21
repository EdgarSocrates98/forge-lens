"""Evaluation result contract (v1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.provenance import Provenance


class EvaluationResult(LensModel):
    """One metric produced by one evaluator for one target."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    evaluator: str
    """Evaluator identity, e.g. ``exact-match``, ``schema-check``."""

    evaluator_version: str | None = None
    dataset: str | None = None
    dataset_version: str | None = None
    metric: str
    score: float | None = None
    confidence: float | None = None
    """Statistical confidence/uncertainty, when the evaluator reports it."""

    ground_truth_reference: str | None = None
    trace_reference: str | None = None
    evaluated_at: datetime | None = None
    details: dict[str, str] = Field(default_factory=dict)
    provenance: Provenance = Provenance.DERIVED
