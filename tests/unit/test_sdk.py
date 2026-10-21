from __future__ import annotations

import pytest

from forge_lens.contracts.v1.trace import SpanKind, StatusCode
from forge_lens.sdk.context import SpanContext
from forge_lens.sdk.exporter import InMemoryExporter
from forge_lens.sdk.tracer import LensTracer, instrument_tool, span


def _tracer() -> tuple[LensTracer, InMemoryExporter]:
    exporter = InMemoryExporter()
    return LensTracer(service_name="test-svc", exporter=exporter), exporter


def test_nested_spans_and_auto_flush() -> None:
    tracer, exporter = _tracer()
    with tracer.start_span("root"), tracer.start_span("child"):
        pass
    # auto-flush on root end
    assert len(exporter.traces) == 1
    trace = exporter.traces[0]
    assert len(trace.spans) == 2
    child = next(s for s in trace.spans if s.name == "child")
    root = next(s for s in trace.spans if s.name == "root")
    assert child.parent_span_id == root.span_id
    assert trace.root_span_id == root.span_id
    assert trace.status.code == StatusCode.OK


def test_error_propagates_to_status() -> None:
    tracer, exporter = _tracer()
    with pytest.raises(ValueError), tracer.start_span("boom"):
        raise ValueError("nope")
    trace = exporter.traces[0]
    assert trace.status.code == StatusCode.ERROR
    assert trace.spans[0].status.code == StatusCode.ERROR
    assert trace.spans[0].events[0].attributes["exception.type"] == "ValueError"


def test_remote_parent_continues_trace() -> None:
    tracer, exporter = _tracer()
    remote = SpanContext.create()
    with tracer.start_span("local-root", remote_parent=remote):
        pass
    trace = exporter.traces[0]
    assert trace.trace_id == remote.trace_id
    assert trace.spans[0].parent_span_id == remote.span_id


def test_decorators() -> None:
    tracer, exporter = _tracer()

    @span("dec", tracer=tracer)
    def work() -> int:
        return 1

    @instrument_tool(name="search", tracer=tracer)
    def search(q: str) -> str:
        return q

    # both inside one root → same trace
    with tracer.agent("run"):
        assert work() == 1
        assert search("x") == "x"

    trace = exporter.traces[0]
    kinds = {s.kind for s in trace.spans}
    assert SpanKind.AGENT in kinds
    assert SpanKind.TOOL in kinds
    assert SpanKind.INTERNAL in kinds


def test_record_usage() -> None:
    tracer, exporter = _tracer()
    with tracer.llm_call("claude-test", provider="anthropic") as s:
        tracer.record_usage(s, input_tokens=10, output_tokens=5, total_tokens=15)
    trace = exporter.traces[0]
    usage = trace.spans[0].attributes["forge_lens.token_usage"]
    assert usage["input_tokens"] == 10
    assert usage["measurement_source"] == "provider"


class _FlakyExporter:
    """Fails on the first N export calls, then records like InMemoryExporter."""

    def __init__(self, failures: int = 1) -> None:
        self.failures = failures
        self.traces: list = []
        self.calls = 0

    def export(self, traces: list, **kw):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionError("endpoint down")
        self.traces.extend(traces)
        return {"accepted_spans": sum(len(t.spans) for t in traces)}


def test_flush_failure_keeps_pending_for_retry() -> None:
    """P0: an export failure must not destroy the pending batch."""
    exporter = _FlakyExporter(failures=1)
    tracer = LensTracer(service_name="t", exporter=exporter, auto_flush=False)
    with tracer.start_span("s"):
        pass
    with pytest.raises(ConnectionError):
        tracer.flush()
    assert tracer._pending  # batch retained
    result = tracer.flush()  # retry succeeds, clears pending
    assert result == {"accepted_spans": 1}
    assert not tracer._pending
    assert len(exporter.traces) == 1


def test_auto_flush_export_failure_is_swallowed() -> None:
    """Observability must never break the observed program."""
    exporter = _FlakyExporter(failures=10)
    tracer = LensTracer(service_name="t", exporter=exporter)
    with tracer.start_span("root"):  # would raise ExportError on root end
        pass
    assert tracer.export_errors == 1
    assert tracer._pending  # telemetry retained for a later flush


def test_auto_flush_never_masks_app_exception() -> None:
    """P0: app raises ValueError; failing export must not replace it."""
    exporter = _FlakyExporter(failures=10)
    tracer = LensTracer(service_name="t", exporter=exporter)
    with pytest.raises(ValueError, match="app-bug"), tracer.start_span("root"):
        raise ValueError("app-bug")
    assert tracer.export_errors == 1


def test_on_export_error_callback_receives_trace() -> None:
    exporter = _FlakyExporter(failures=10)
    seen: list[tuple[Exception, object]] = []
    tracer = LensTracer(
        service_name="t",
        exporter=exporter,
        on_export_error=lambda exc, trace: seen.append((exc, trace)),
    )
    with tracer.start_span("root"):
        pass
    assert len(seen) == 1
    exc, trace = seen[0]
    assert isinstance(exc, ConnectionError)
    assert trace.trace_id == next(iter(tracer._pending))


def test_invalid_trace_id_is_flagged_not_silently_replaced() -> None:
    """Producer bugs stay visible: bad trace_id → span flagged + new id."""
    tracer, exporter = _tracer()
    with tracer.start_span("s", trace_id="not-a-valid-id") as s:
        pass
    assert s.attributes["forge_lens.trace_id_regenerated"] is True
    assert s.attributes["forge_lens.invalid_trace_id_supplied"] == "not-a-valid-id"
    trace = exporter.traces[0]
    assert trace.trace_id != "not-a-valid-id"
