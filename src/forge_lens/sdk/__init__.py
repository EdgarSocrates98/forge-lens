"""Forge Lens Python SDK — instrument agents, emit traces; query the API."""

from forge_lens.sdk.query import LensClient, QueryError
from forge_lens.sdk.tracer import (
    LensTracer,
    get_tracer,
    instrument_tool,
    span,
)

__all__ = [
    "LensClient",
    "LensTracer",
    "QueryError",
    "get_tracer",
    "instrument_tool",
    "span",
]
