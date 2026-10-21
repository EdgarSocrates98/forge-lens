"""Token usage and cost observation contracts (v1)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.provenance import Provenance


class MeasurementSource(StrEnum):
    """Where token counts came from."""

    PROVIDER = "provider"
    """Reported by the model provider's API response."""

    ESTIMATED = "estimated"
    """Computed locally (tokenizer/heuristic)."""

    UNKNOWN = "unknown"


class TokenUsage(LensModel):
    """Token counters for one model invocation.

    Absent counters stay ``None`` — never invent precision the provider
    did not report.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    provider: str | None = None
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_input_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    measurement_source: MeasurementSource = MeasurementSource.UNKNOWN
    provenance: Provenance = Provenance.OBSERVED


class CostObservation(LensModel):
    """A cost estimate or a provider-reported billed cost.

    ``estimated_cost`` and ``billed_cost`` are different epistemic kinds:
    an estimate is DERIVED from a pricing registry, a billed cost is
    OBSERVED from a provider report. They are never merged.
    """

    schema_version: Literal["1.0.0"] = "1.0.0"
    currency: str = "USD"
    """ISO-4217 currency code."""

    model: str | None = None
    provider: str | None = None
    pricing_source: str | None = None
    """Where the price came from (e.g. ``openai-pricing-2026-10``)."""

    pricing_version: str | None = None
    usage: TokenUsage | None = None
    estimated_cost: float | None = None
    billed_cost: float | None = None
    """Only when the provider actually reported it."""

    assumptions: list[str] = Field(default_factory=list)
    """Explicit premises used to derive the estimate."""

    provenance: Provenance = Provenance.DERIVED
