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
