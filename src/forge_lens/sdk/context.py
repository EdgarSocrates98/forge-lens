"""Context propagation — W3C traceparent + ContextVar-based parent stack."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass

from forge_lens.core.ids import (
    format_traceparent,
    new_span_id,
    new_trace_id,
    parse_traceparent,
)


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str
    sampled: bool = True

    @classmethod
    def create(cls, trace_id: str | None = None) -> SpanContext:
        return cls(trace_id=trace_id or new_trace_id(), span_id=new_span_id())

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> SpanContext | None:
        tp = headers.get("traceparent")
        if not tp:
            return None
        parsed = parse_traceparent(tp)
        if parsed is None:
            return None
        trace_id, span_id, sampled = parsed
        return cls(trace_id=trace_id, span_id=span_id, sampled=sampled)

    def to_headers(self) -> dict[str, str]:
        return {"traceparent": format_traceparent(self.trace_id, self.span_id, self.sampled)}


_current: contextvars.ContextVar[tuple[SpanContext, ...]] = contextvars.ContextVar(
    "forge_lens_span_stack", default=()
)


def current_span() -> SpanContext | None:
    stack = _current.get()
    return stack[-1] if stack else None


def push_span(ctx: SpanContext) -> contextvars.Token[tuple[SpanContext, ...]]:
    return _current.set((*_current.get(), ctx))


def pop_span(token: contextvars.Token[tuple[SpanContext, ...]]) -> None:
    _current.reset(token)
