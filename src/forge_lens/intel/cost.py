"""Trace-level token + cost report derived from observed usage."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.v1.trace import Span, SpanKind, Trace
from forge_lens.contracts.v1.usage import TokenUsage
from forge_lens.intel.pricing import PricingRegistry

USAGE_ATTR = "forge_lens.token_usage"


class SpanCost(LensModel):
    """Derived cost view for one span — DERIVED provenance, always."""

    span_id: str
    name: str
    kind: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    priced: bool = False
    estimated_usd: float | None = None
    """None when the model is absent from the registry."""


class CostReport(LensModel):
    """Aggregated, evidence-linked cost view of a trace."""

    trace_id: str
    project_id: str
    span_costs: list[SpanCost] = Field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tokens: int = 0
    total_estimated_usd: float = 0.0
    by_model: dict[str, dict[str, Any]] = Field(default_factory=dict)
    unpriced_models: list[str] = Field(default_factory=list)
    usage_missing_on_llm_spans: list[str] = Field(default_factory=list)
    """LLM-kind spans carrying no token_usage — honest gap, not zero."""
    assumptions: list[str] = Field(default_factory=list)
    provenance: str = "derived"


def extract_usage(span: Span) -> TokenUsage | None:
    """Read ``forge_lens.token_usage`` tolerantly; bad shape → None."""
    raw = span.attributes.get(USAGE_ATTR)
    if not isinstance(raw, dict):
        return None
    try:
        return TokenUsage.model_validate(raw)
    except Exception:  # malformed producer data: skip, don't crash
        return None


def trace_cost(trace: Trace, registry: PricingRegistry) -> CostReport:
    """Derive a :class:`CostReport` for one trace.

    Deterministic: spans processed in ``(start_time, span_id)`` order;
    totals only ever *add* observed counters — None stays None per span,
    never invented as zero in the report's per-span rows.
    """
    report = CostReport(trace_id=trace.trace_id, project_id=trace.project_id)
    seen_assumptions: set[str] = set()

    for span in sorted(trace.spans, key=lambda s: (s.start_time, s.span_id)):
        usage = extract_usage(span)
        if usage is None:
            if span.kind is SpanKind.LLM:
                report.usage_missing_on_llm_spans.append(span.span_id)
            continue
        usd, assumptions = registry.estimate(
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        )
        seen_assumptions.update(assumptions)
        row = SpanCost(
            span_id=span.span_id,
            name=span.name,
            kind=span.kind.value,
            model=usage.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cached_input_tokens=usage.cached_input_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            total_tokens=usage.total_tokens,
            priced=usd is not None,
            estimated_usd=round(usd, 8) if usd is not None else None,
        )
        report.span_costs.append(row)
        report.total_input_tokens += usage.input_tokens or 0
        report.total_output_tokens += usage.output_tokens or 0
        report.total_tokens += usage.total_tokens or (
            (usage.input_tokens or 0) + (usage.output_tokens or 0)
        )
        if usd is not None:
            report.total_estimated_usd += usd
            bucket = report.by_model.setdefault(
                usage.model or "unknown",
                {"input_tokens": 0, "output_tokens": 0, "estimated_usd": 0.0},
            )
            bucket["input_tokens"] += usage.input_tokens or 0
            bucket["output_tokens"] += usage.output_tokens or 0
            bucket["estimated_usd"] += usd
        elif usage.model:
            if usage.model not in report.unpriced_models:
                report.unpriced_models.append(usage.model)

    report.total_estimated_usd = round(report.total_estimated_usd, 8)
    for bucket in report.by_model.values():
        bucket["estimated_usd"] = round(bucket["estimated_usd"], 8)
    report.assumptions = sorted(seen_assumptions)
    return report
