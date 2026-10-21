"""Versioned evaluation datasets — Cycle 06 contracts.

A dataset is an immutable-by-version collection of ground-truth cases.
Versions are append-only: a case stored under ``version`` is never
mutated in place (new truth → new version), so an eval run pinned to
``dataset@version`` is reproducible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from forge_lens.contracts.base import LensModel


class DatasetCase(LensModel):
    """One ground-truth case inside a dataset version."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    case_id: str = Field(min_length=1, max_length=256)
    input: dict[str, Any] = Field(default_factory=dict)
    """Task inputs (prompt spec, tool config, context refs)."""
    expected: dict[str, Any] = Field(default_factory=dict)
    """Ground truth the evaluator compares against — arbitrary shape."""
    allowed_tools: list[str] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    rubric: dict[str, Any] = Field(default_factory=dict)


class Dataset(LensModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str = Field(min_length=1, max_length=256)
    project_id: str = "default"
    name: str = Field(min_length=1, max_length=512)
    task: str | None = None
    created_at: datetime | None = None
    """Write-time metadata — supplied by the writer and stored verbatim so
    dataset exports/imports keep their provenance; NULL means unobserved.
    For server-issued records the server supplies it."""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DatasetVersion(LensModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    dataset_id: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=64)
    description: str | None = None
    created_at: datetime | None = None
    case_count: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    cases: list[DatasetCase] = Field(default_factory=list)
