"""Persisted A/B experiments — Cycle 13 contracts (audit gap 06.3).

An experiment pins the *spec* of a comparison — two declared cohorts of
stored traces plus the evaluator specs — so eval/compare runs are
reproducible instead of ephemeral. Cohort ``config`` carries declared
labels (agent/model/prompt/tool versions); the platform stores them
verbatim and never resolves them into claims about the world.

Runs are append-only evidence: each execution appends a row to
``experiment_results`` and advances the honest lifecycle
``open → completed`` — an experiment never silently loses a run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.provenance import Provenance


class ExperimentCohort(LensModel):
    """One side of an A/B experiment — a declared set of trace ids."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    trace_ids: list[str] = Field(default_factory=list, max_length=500)
    """Pinned stored-trace ids for this side (bounded like eval/compare)."""
    label: str | None = Field(default=None, max_length=256)
    config: dict[str, Any] = Field(default_factory=dict)
    """Declared agent/model/prompt/tool versions — labels, never resolved."""


class ExperimentCohorts(LensModel):
    """The two sides of the comparison — ``a`` baseline, ``b`` candidate."""

    a: ExperimentCohort = Field(default_factory=ExperimentCohort)
    b: ExperimentCohort = Field(default_factory=ExperimentCohort)


class Experiment(LensModel):
    """A persisted, re-runnable A/B evaluation spec."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    experiment_id: str = Field(min_length=1, max_length=256)
    project_id: str = "default"
    name: str | None = Field(default=None, max_length=512)
    description: str | None = None
    created_at: datetime | None = None
    """Write-time metadata — same convention as ``Dataset.created_at``:
    supplied by the writer and stored verbatim; NULL means unobserved."""
    dataset_id: str | None = Field(default=None, max_length=256)
    dataset_version: str | None = Field(default=None, max_length=64)
    """Pinned dataset truth this experiment evaluates against, if any."""
    cohorts: ExperimentCohorts = Field(default_factory=ExperimentCohorts)
    evaluators: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    """Evaluator spec list in the same shape as ``/v1/eval/compare``'s
    ``evaluators`` (``{name, params}`` dicts)."""
    result_ref: str | None = None
    """Reference to the latest persisted run (``run:<id>``) or null when
    the experiment has never executed — derived state, never declared."""
    status: Literal["open", "completed"] = "open"
    """Honest lifecycle: ``open`` until a run is recorded, ``completed``
    after. Re-spec'ing keeps prior runs — history is append-only."""
    provenance: Provenance = Provenance.IMPORTED
    """The spec is declared by an external caller, not observed from a
    live execution — IMPORTED is the honest classification."""
    metadata: dict[str, Any] = Field(default_factory=dict)
