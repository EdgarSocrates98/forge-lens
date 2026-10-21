"""LensTracer — the main SDK entry point.

Design: spans are accumulated in memory per trace and flushed as a
``Trace`` through an exporter. A trace is auto-flushed when its root
span ends, or on explicit ``flush()``. This keeps ingestion atomic
per-trace instead of per-span, which avoids partial-tree reads for the
common case. Long-running traces can call ``flush()`` mid-flight —
the store merges subsequent flushes.
"""

from __future__ import annotations

import functools
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, ParamSpec, TypeVar

from forge_lens import __version__
from forge_lens.contracts.v1.trace import (
    Span,
    SpanEvent,
    SpanKind,
    Status,
    StatusCode,
    Trace,
    TraceSource,
)
from forge_lens.contracts.v1.usage import MeasurementSource, TokenUsage
from forge_lens.core.ids import is_valid_trace_id
from forge_lens.sdk.context import (
    SpanContext,
    current_span,
    pop_span,
    push_span,
)

P = ParamSpec("P")
R = TypeVar("R")

FlushCallback = Callable[[Trace], None]
ExportErrorCallback = Callable[[Exception, "Trace"], None]


class LensTracer:
    """Build traces and spans; flush to an exporter or a callback.

    Observability must never change the observed program's behavior:
    auto-flush export failures are swallowed (counted on
    ``export_errors`` and reported via ``on_export_error``), and failed
    batches stay in ``_pending`` so a later ``flush()`` retries them.
    """

    def __init__(
        self,
        *,
        service_name: str = "forge-lens-app",
        exporter: Any | None = None,
        on_flush: FlushCallback | None = None,
        on_export_error: ExportErrorCallback | None = None,
        auto_flush: bool = True,
        session_id: str | None = None,
    ) -> None:
        self.source = TraceSource(
            system=service_name,
            sdk_name="forge-lens-sdk-python",
            sdk_version=__version__,
            runtime=f"python-{sys.version_info.major}.{sys.version_info.minor}",
            language="python",
        )
        from forge_lens.sdk.exporter import InMemoryExporter

        self.exporter = exporter or InMemoryExporter()
        self.on_flush = on_flush
        self.on_export_error = on_export_error
        self.auto_flush = auto_flush
        self.session_id = session_id
        self.export_errors = 0
        self._pending: dict[str, Trace] = {}
        self._root_ctx: dict[str, SpanContext] = {}

    # -- span lifecycle -------------------------------------------------

    @contextmanager
    def start_span(
        self,
        name: str,
        *,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: dict[str, Any] | None = None,
        trace_id: str | None = None,
        remote_parent: SpanContext | None = None,
    ) -> Iterator[Span]:
        """Start a span; parent comes from context stack (or remote_parent)."""
        parent = remote_parent or current_span()
        tid = trace_id or (parent.trace_id if parent else None)
        attrs = dict(attributes) if attributes else {}
        if tid and not is_valid_trace_id(tid):
            # flag, don't silently fix: record the discarded producer id
            attrs["forge_lens.trace_id_regenerated"] = True
            attrs["forge_lens.invalid_trace_id_supplied"] = str(tid)[:200]
            tid = None
        ctx = SpanContext.create(tid)
        span_obj = Span(
            trace_id=ctx.trace_id,
            span_id=ctx.span_id,
            parent_span_id=parent.span_id if parent else None,
            name=name,
            kind=kind,
            start_time=datetime.now(tz=UTC),
            attributes=attrs,
        )
        trace = self._trace_for(ctx, parent is None or remote_parent is not None)
        trace.spans.append(span_obj)
        token = push_span(ctx)
        try:
            yield span_obj
            if span_obj.status.code == StatusCode.UNSET:
                span_obj.status = Status(code=StatusCode.OK)
        except Exception as exc:
            span_obj.status = Status(code=StatusCode.ERROR, message=str(exc)[:500])
            span_obj.events.append(
                SpanEvent(
                    name="exception",
                    time=datetime.now(tz=UTC),
                    attributes={
                        "exception.type": type(exc).__name__,
                        "exception.message": str(exc)[:1000],
                    },
                )
            )
            raise
        finally:
            span_obj.end_time = datetime.now(tz=UTC)
            pop_span(token)
            self._maybe_flush(trace, ctx)

    def span(self, name: str, **kwargs: Any) -> Any:
        """Alias of start_span for ergonomic use."""
        return self.start_span(name, **kwargs)

    # -- semantic helpers -----------------------------------------------

    @contextmanager
    def agent(self, name: str, **attrs: Any) -> Iterator[Span]:
        with self.start_span(name, kind=SpanKind.AGENT, attributes=attrs) as s:
            yield s

    @contextmanager
    def llm_call(
        self,
        model: str,
        *,
        provider: str | None = None,
        **attrs: Any,
    ) -> Iterator[Span]:
        attrs.setdefault("llm.model_name", model)
        if provider:
            attrs.setdefault("llm.provider", provider)
        with self.start_span(f"llm {model}", kind=SpanKind.LLM, attributes=attrs) as s:
            yield s

    def record_usage(
        self,
        span_obj: Span,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cached_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        total_tokens: int | None = None,
        model: str | None = None,
        provider: str | None = None,
        source: MeasurementSource | str = MeasurementSource.PROVIDER,
    ) -> None:
        """Attach provider-reported (or estimated) token counters to a span.

        ``source`` accepts a ``MeasurementSource`` or its string value
        (``"provider"``, ``"estimated"``, ``"unknown"``).
        """
        usage = TokenUsage(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            measurement_source=MeasurementSource(source),
        )
        span_obj.attributes["forge_lens.token_usage"] = usage.model_dump(
            mode="json", exclude_none=True
        )

    @contextmanager
    def tool_call(
        self,
        tool_name: str,
        *,
        arguments: dict[str, Any] | None = None,
        capture_args: bool = False,
        **attrs: Any,
    ) -> Iterator[Span]:
        """Track a tool invocation.

        Arguments are recorded only when ``capture_args=True`` —
        mirroring the capture-level policy (metadata by default).
        """
        attrs.setdefault("tool.name", tool_name)
        if capture_args and arguments is not None:
            attrs["tool.parameters"] = json.dumps(arguments, default=str)[:4096]
        with self.start_span(f"tool {tool_name}", kind=SpanKind.TOOL, attributes=attrs) as s:
            yield s

    # -- flush ----------------------------------------------------------

    def _trace_for(self, ctx: SpanContext, is_root: bool) -> Trace:
        trace = self._pending.get(ctx.trace_id)
        if trace is None:
            trace = Trace(
                trace_id=ctx.trace_id,
                session_id=self.session_id,
                started_at=datetime.now(tz=UTC),
                source=self.source,
                spans=[],
            )
            self._pending[ctx.trace_id] = trace
        if is_root:
            self._root_ctx[ctx.trace_id] = ctx
            trace.root_span_id = ctx.span_id
        return trace

    def _maybe_flush(self, trace: Trace, ctx: SpanContext) -> None:
        root = self._root_ctx.get(ctx.trace_id)
        if self.auto_flush and root is not None and root.span_id == ctx.span_id:
            try:
                self.flush(ctx.trace_id)
            except Exception as exc:  # never let export break the host
                self.export_errors += 1
                if self.on_export_error is not None:
                    try:
                        self.on_export_error(exc, trace)
                    except Exception:
                        self.export_errors += 1

    def flush(self, trace_id: str | None = None) -> dict[str, Any] | None:
        """Export pending traces. Failed batches stay pending for retry.

        Raises the exporter's error to the *explicit* caller — only the
        auto-flush path swallows it.
        """
        ids = [trace_id] if trace_id else list(self._pending.keys())
        batch: list[Trace] = []
        for tid in ids:
            trace = self._pending.get(tid)
            if trace is not None:
                _finalize(trace)
                batch.append(trace)
        if not batch:
            return None
        result = self.exporter.export(batch, source=self.source)
        for trace in batch:
            self._pending.pop(trace.trace_id, None)
            self._root_ctx.pop(trace.trace_id, None)
        if self.on_flush:
            for trace in batch:
                self.on_flush(trace)
        return result


def _finalize(trace: Trace) -> None:
    if trace.spans:
        trace.started_at = min(s.start_time for s in trace.spans)
        ended = [s.end_time for s in trace.spans if s.end_time]
        trace.ended_at = max(ended) if ended else None
    if any(s.status.code == StatusCode.ERROR for s in trace.spans):
        trace.status = Status(code=StatusCode.ERROR)
    elif all(s.status.code == StatusCode.OK for s in trace.spans):
        trace.status = Status(code=StatusCode.OK)


_default: LensTracer | None = None


def get_tracer(service_name: str = "forge-lens-app", **kwargs: Any) -> LensTracer:
    """Process-wide default tracer (lazy singleton)."""
    global _default
    if _default is None:
        _default = LensTracer(service_name=service_name, **kwargs)
    return _default


def span(
    name: str | None = None,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    tracer: LensTracer | None = None,
) -> Any:
    """Decorator: wrap a function in a span.

    Usage::

        @span("plan")
        def plan(...): ...

        @span(kind=SpanKind.LLM)
        def call_model(...): ...
    """
    def decorator(fn: Callable[P, R]) -> Callable[P, R]:
        span_name = name or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args: P.args, **kw: P.kwargs) -> R:
            t = tracer or get_tracer()
            with t.start_span(span_name, kind=kind):
                return fn(*args, **kw)

        return wrapper

    return decorator


def instrument_tool(
    fn: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    capture_args: bool = False,
    tracer: LensTracer | None = None,
) -> Any:
    """Decorator: mark a function as a tool invocation."""

    def decorator(f: Callable[P, R]) -> Callable[P, R]:
        tool_name = name or f.__name__

        @functools.wraps(f)
        def wrapper(*args: P.args, **kw: P.kwargs) -> R:
            t = tracer or get_tracer()
            with t.tool_call(tool_name, capture_args=capture_args):
                return f(*args, **kw)

        return wrapper

    if fn is not None:
        return decorator(fn)
    return decorator
