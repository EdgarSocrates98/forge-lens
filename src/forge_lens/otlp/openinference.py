"""OpenInference semantic-convention mapping.

Maps ``openinference.*`` span attributes to Forge Lens concepts. Only
attributes actually observed are mapped — no fabricated fields.
Reference: OpenInference tracing specification (openinference-span-kind
attribute conventions).
"""

from __future__ import annotations

from typing import Any

from forge_lens.contracts.v1.trace import SpanKind
from forge_lens.contracts.v1.usage import MeasurementSource, TokenUsage

OI_SPAN_KIND = "openinference.span.kind"

OI_KIND_MAP: dict[str, SpanKind] = {
    "AGENT": SpanKind.AGENT,
    "CHAIN": SpanKind.CHAIN,
    "LLM": SpanKind.LLM,
    "TOOL": SpanKind.TOOL,
    "RETRIEVER": SpanKind.RETRIEVER,
    "EMBEDDING": SpanKind.EMBEDDING,
    "RERANKER": SpanKind.RERANKER,
    "GUARDRAIL": SpanKind.GUARDRAIL,
    "EVALUATOR": SpanKind.EVALUATOR,
    "UNKNOWN": SpanKind.OTHER,
}

# Token attributes seen in the wild, both OpenInference and OTel GenAI semconv.
_INPUT_KEYS = (
    "llm.token_count.prompt",
    "gen_ai.usage.input_tokens",
    "gen_ai.usage.prompt_tokens",
    "llm.usage.prompt_tokens",
)
_OUTPUT_KEYS = (
    "llm.token_count.completion",
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
    "llm.usage.completion_tokens",
)
_TOTAL_KEYS = (
    "llm.token_count.total",
    "gen_ai.usage.total_tokens",
)
_CACHED_KEYS = (
    "llm.token_count.prompt_details.cache_read",
    "llm.token_count.prompt_cache_hit",
    "gen_ai.usage.cached_input_tokens",
)
_REASONING_KEYS = (
    "llm.token_count.completion_details.reasoning",
    "gen_ai.usage.reasoning_tokens",
)

_MODEL_KEYS = (
    "llm.model_name",
    "gen_ai.request.model",
    "gen_ai.response.model",
    "llm.invocation_parameters.model",
)
_PROVIDER_KEYS = ("llm.provider", "gen_ai.system", "llm.system")

# Content-bearing attributes that must only be captured at level=full.
CONTENT_ATTRIBUTES = frozenset(
    {
        "input.value",
        "output.value",
        "llm.input_messages",
        "llm.output_messages",
        "llm.prompts",
        "tool.parameters",
        "retrieval.documents",
        "input.mime_type",
        "output.mime_type",
    }
)


def _first_int(attrs: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _first_str(attrs: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = attrs.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def map_span_kind(attributes: dict[str, Any], otel_kind: int | None) -> SpanKind:
    """Resolve effective span kind: OpenInference kind wins over OTel kind."""
    oi = attributes.get(OI_SPAN_KIND)
    if isinstance(oi, str):
        mapped = OI_KIND_MAP.get(oi.upper())
        if mapped is not None:
            return mapped
    otel_map = {
        1: SpanKind.INTERNAL,
        2: SpanKind.SERVER,
        3: SpanKind.CLIENT,
        4: SpanKind.PRODUCER,
        5: SpanKind.CONSUMER,
    }
    return otel_map.get(otel_kind or 0, SpanKind.INTERNAL)


def extract_token_usage(attributes: dict[str, Any]) -> TokenUsage | None:
    """Build a TokenUsage from span attributes when any counter is present."""
    input_t = _first_int(attributes, _INPUT_KEYS)
    output_t = _first_int(attributes, _OUTPUT_KEYS)
    total_t = _first_int(attributes, _TOTAL_KEYS)
    cached_t = _first_int(attributes, _CACHED_KEYS)
    reasoning_t = _first_int(attributes, _REASONING_KEYS)
    model = _first_str(attributes, _MODEL_KEYS)
    provider = _first_str(attributes, _PROVIDER_KEYS)
    if all(v is None for v in (input_t, output_t, total_t, cached_t, reasoning_t)):
        return None
    return TokenUsage(
        provider=provider,
        model=model,
        input_tokens=input_t,
        output_tokens=output_t,
        cached_input_tokens=cached_t,
        reasoning_tokens=reasoning_t,
        total_tokens=total_t,
        measurement_source=MeasurementSource.PROVIDER,
    )
