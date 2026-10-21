from __future__ import annotations

import pytest

from forge_lens.contracts.v1.trace import SpanKind, StatusCode
from forge_lens.sdk.context import SpanContext
from forge_lens.sdk.exporter import InMemoryExporter
from forge_lens.sdk.tracer import LensTracer, instrument_tool, span


def _tracer() -> tuple[LensTracer, InMemoryExporter]:
    exporter = InMemoryExporter()
    return (
        LensTracer(
            service_name="test-svc", exporter=exporter, register_atexit=False
        ),
        exporter,
    )


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
    tracer = LensTracer(
        service_name="t", exporter=exporter, auto_flush=False, register_atexit=False
    )
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
    tracer = LensTracer(
        service_name="t", exporter=exporter, register_atexit=False,
        max_export_failures=100,
    )
    with tracer.start_span("root"):  # would raise ExportError on root end
        pass
    assert tracer.export_errors == 1
    assert tracer._pending  # telemetry retained for a later flush


def test_auto_flush_never_masks_app_exception() -> None:
    """P0: app raises ValueError; failing export must not replace it."""
    exporter = _FlakyExporter(failures=10)
    tracer = LensTracer(
        service_name="t", exporter=exporter, register_atexit=False,
        max_export_failures=100,
    )
    with pytest.raises(ValueError, match="app-bug"), tracer.start_span("root"):
        raise ValueError("app-bug")
    assert tracer.export_errors == 1


def test_on_export_error_callback_receives_trace() -> None:
    exporter = _FlakyExporter(failures=10)
    seen: list[tuple[Exception, object]] = []
    tracer = LensTracer(
        service_name="t",
        exporter=exporter,
        register_atexit=False,
        max_export_failures=100,
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


def test_mid_flight_flush_keeps_root_auto_flush() -> None:
    """P0: flush() mid-trace must not unregister the root. When the root
    later ends, auto-flush must still fire — nothing stranded pending."""
    tracer, exporter = _tracer()
    with tracer.start_span("root"):
        with tracer.start_span("child1"):
            pass
        tracer.flush()  # documented mid-flight flush (partial delivery)
        assert len(exporter.traces) == 1
        assert exporter.traces[0].spans  # partial trace went out
        with tracer.start_span("child2"):
            pass
    # root ended → final flush must have fired automatically
    assert len(exporter.traces) == 2
    final = exporter.traces[-1]
    names = {s.name for s in final.spans}
    assert {"root", "child1", "child2"} <= names
    assert not tracer._pending


def test_shutdown_flushes_incomplete_traces() -> None:
    """shutdown() is the terminal flush: even a root still open exports.
    When the root later ends, auto-flush still delivers the final merged
    version (the store merges partial deliveries)."""
    tracer, exporter = _tracer()
    with tracer.start_span("root"):
        with tracer.start_span("child"):
            pass
        # root still open; nothing exported yet (auto-flush fires on end)
        assert not exporter.traces
        tracer.shutdown()
        assert len(exporter.traces) == 1  # partial went out
    assert len(exporter.traces) == 2  # final delivery on root end
    assert {s.name for s in exporter.traces[-1].spans} == {"root", "child"}
    assert not tracer._pending


def test_instrument_tool_capture_args_records_parameters() -> None:
    """capture_args=True must actually forward arguments → tool.parameters."""
    tracer, exporter = _tracer()

    @instrument_tool(name="search", capture_args=True, tracer=tracer)
    def search(q: str, limit: int = 3) -> str:
        return q

    @instrument_tool(name="quiet", capture_args=False, tracer=tracer)
    def quiet(secret: str) -> str:
        return secret

    with tracer.start_span("root"):
        assert search("hello", limit=5) == "hello"
        quiet("nope")

    tracer.flush()
    spans = [s for t in exporter.traces for s in t.spans]
    tool_span = next(s for s in spans if s.name == "tool search")
    import json as _json

    params = _json.loads(tool_span.attributes["tool.parameters"])
    assert params["args"] == ["hello"]
    assert params["kwargs"] == {"limit": 5}
    quiet_span = next(s for s in spans if s.name == "tool quiet")
    assert "tool.parameters" not in quiet_span.attributes


class _PoisonAwareExporter:
    """Fails any batch containing a span named 'poison'; accepts the rest."""

    def __init__(self) -> None:
        self.traces: list = []

    def export(self, traces: list, **kw):  # type: ignore[no-untyped-def]
        for t in traces:
            if any(s.name == "poison" for s in t.spans):
                raise RuntimeError("simulated permanent rejection (413)")
        self.traces.extend(traces)
        return {"accepted_spans": sum(len(t.spans) for t in traces)}


def test_poison_batch_salvages_good_traces() -> None:
    """A permanently-failing trace must not block the others in a batch:
    the salvage pass re-exports each alone."""
    exporter = _PoisonAwareExporter()
    tracer = LensTracer(
        service_name="t", exporter=exporter, auto_flush=False,
        register_atexit=False, max_export_failures=3,
    )
    with tracer.start_span("good"):
        pass
    with tracer.start_span("poison"):
        pass
    with pytest.raises(RuntimeError):
        tracer.flush()
    # salvage pass delivered the good trace even though the batch failed
    assert [s.name for t in exporter.traces for s in t.spans] == ["good"]
    assert len(tracer._pending) == 1  # only the poison stays pending


def test_persistent_failure_dead_letters_trace() -> None:
    """After max_export_failures the trace leaves pending explicitly —
    into dead_letters + on_dead_letter — never silently."""
    exporter = _FlakyExporter(failures=99)
    dead: list = []
    tracer = LensTracer(
        service_name="t", exporter=exporter, auto_flush=False,
        register_atexit=False, max_export_failures=2,
        on_dead_letter=dead.append,
    )
    with tracer.start_span("doomed"):
        pass
    with pytest.raises(ConnectionError):
        tracer.flush()
    assert len(tracer._pending) == 1
    with pytest.raises(ConnectionError):
        tracer.flush()
    assert not tracer._pending
    assert len(dead) == 1
    assert dead[0].spans[0].name == "doomed"
    assert tracer.dead_letters == dead


def test_concurrent_spans_during_export_are_not_lost() -> None:
    """A span appended while an export is in flight must survive: the
    lock serializes append-vs-export, so it either rides the in-flight
    snapshot or stays pending for the next flush — never dropped."""
    import threading
    from contextlib import ExitStack

    class _SlowExporter:
        def __init__(self) -> None:
            self.traces: list = []
            self.entered = threading.Event()
            self.release = threading.Event()

        def export(self, traces: list, **kw):  # type: ignore[no-untyped-def]
            self.entered.set()
            self.release.wait(timeout=5)
            self.traces.extend(traces)
            return {}

    exporter = _SlowExporter()
    tracer = LensTracer(
        service_name="t", exporter=exporter, auto_flush=False,
        register_atexit=False,
    )
    stack = ExitStack()
    root_span = stack.enter_context(tracer.start_span("root"))
    try:
        flush_thread = threading.Thread(target=tracer.flush)
        flush_thread.start()
        assert exporter.entered.wait(timeout=5)

        def add_span() -> None:
            # worker thread: no contextvar parent — joins the trace via id
            with tracer.start_span(
                "during-flight", trace_id=root_span.trace_id
            ):
                pass

        add_thread = threading.Thread(target=add_span)
        add_thread.start()
        exporter.release.set()
        flush_thread.join(timeout=5)
        add_thread.join(timeout=5)
        assert not add_thread.is_alive()
    finally:
        stack.close()
    tracer.flush()  # collect whatever the in-flight snapshot missed
    names = {s.name for tr in exporter.traces for s in tr.spans}
    assert "during-flight" in names
    assert not tracer._pending
