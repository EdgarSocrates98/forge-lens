"""End-to-end: instrumented run → HTTP ingest → persisted → retrievable.

This is the Cycle 01 quality gate exercised in code: a *real* (simulated
agent) execution flows through the SDK, the API and the store, and the
trace can be read back with evidence intact.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge_lens.api.app import create_app
from forge_lens.contracts.v1.trace import Trace
from forge_lens.core.config import LensConfig
from forge_lens.sdk.exporter import HTTPExporter
from forge_lens.sdk.tracer import LensTracer
from forge_lens.storage.sqlite import SQLiteTraceStore

pytestmark = pytest.mark.e2e


def simulate_agent_run(tracer: LensTracer) -> None:
    """A mini agent: plan → llm → tool → llm (with an error)."""
    with tracer.agent("research-agent", **{"agent.role": "researcher"}):
        with tracer.llm_call("claude-local", provider="anthropic") as s:
            tracer.record_usage(s, input_tokens=240, output_tokens=60, total_tokens=300)
        with tracer.tool_call("local_search", capture_args=True, arguments={"q": "forge lens"}):
            pass
        try:
            with tracer.tool_call("write_file"):
                raise RuntimeError("disk full")
        except RuntimeError:
            pass  # agent catches tool error and finishes


def test_sdk_to_api_to_store(tmp_path) -> None:
    db = tmp_path / "e2e.db"
    store = SQLiteTraceStore(db)
    app = create_app(store=store, config=LensConfig(data_dir=tmp_path, db_path=db))

    # HTTPExporter points at the TestClient's transport by monkeypatching urlopen?
    # No — we run a real exporter against a real in-process ASGI transport is
    # not possible with urllib, so we post the envelope with TestClient, and
    # exercise HTTPExporter serialization separately (see test_exporter_body).
    with TestClient(app) as client:
        traces: list[Trace] = []
        tracer = LensTracer(
            service_name="e2e-agent",
            on_flush=lambda t: traces.append(t),
        )
        # in-memory exporter then POST the same envelope the HTTPExporter builds
        simulate_agent_run(tracer)
        assert len(traces) == 1

        from forge_lens.contracts.v1.trace import TraceIngest

        envelope = TraceIngest(
            project_id="e2e", source=tracer.source, traces=traces
        ).model_dump(mode="json")
        r = client.post("/v1/traces", json=envelope)
        assert r.status_code == 200, r.text
        assert r.json()["accepted_spans"] == 4

        tid = traces[0].trace_id
        r = client.get(f"/v1/traces/{tid}", params={"project_id": "e2e"})
        assert r.status_code == 200
        body = r.json()
        assert len(body["spans"]) == 4

        spans = {s["name"]: s for s in body["spans"]}
        assert spans["tool write_file"]["status"]["code"] == "error"
        llm = next(s for s in body["spans"] if s["kind"] == "llm")
        assert llm["attributes"]["forge_lens.token_usage"]["total_tokens"] == 300
        # tool args captured because capture_args=True at SDK level
        tool = spans["tool local_search"]
        assert "tool.parameters" in tool["attributes"]

        r = client.get("/v1/traces", params={"project_id": "e2e"})
        assert any(i["trace_id"] == tid for i in r.json()["items"])

        stats = store.stats()
        assert stats["spans"] == 4
    store.close()


def test_otlp_file_to_store(tmp_path, otlp_payload: dict) -> None:
    """OTLP JSON file → normalize → pipeline → store (CLI ingest path)."""
    from forge_lens.ingest.pipeline import IngestPipeline
    from forge_lens.otlp.normalize import normalize_otlp_request

    db = tmp_path / "otlp.db"
    store = SQLiteTraceStore(db)
    cfg = LensConfig(data_dir=tmp_path, db_path=db)
    pipeline = IngestPipeline(store, cfg)

    traces = normalize_otlp_request(otlp_payload, project_id=cfg.project_id)
    result = pipeline.ingest(traces)
    assert result.accepted_spans == 2

    loaded = store.get_trace("b" * 32)
    assert loaded is not None
    llm = next(s for s in loaded.spans if s.name == "llm-call")
    assert llm.attributes["forge_lens.token_usage"]["input_tokens"] == 512
    store.close()


def test_http_exporter_over_real_socket(tmp_path) -> None:
    """Real TCP round-trip: HTTPExporter → uvicorn → SQLite.

    The flush-failure regressions only survived because every prior test
    used TestClient/mocked urlopen — this exercises the real socket path.
    """
    import socket
    import threading
    import time

    import uvicorn

    db = tmp_path / "live.db"
    store = SQLiteTraceStore(db)
    app = create_app(store=store, config=LensConfig(data_dir=tmp_path, db_path=db))

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started, "uvicorn did not start"

        exporter = HTTPExporter(f"http://127.0.0.1:{port}", project_id="live")
        tracer = LensTracer(service_name="live-agent", exporter=exporter)
        simulate_agent_run(tracer)  # auto-flush over the wire on root end

        # flush is synchronous for the py SDK — the trace must be stored
        items = store.list_traces(project_id="live")
        assert len(items) == 1
        loaded = store.get_trace(items[0]["trace_id"], project_id="live")
        assert loaded is not None
        assert len(loaded.spans) == 4
        assert any(s.status.code.value == "error" for s in loaded.spans)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        store.close()


def test_exporter_body_shape() -> None:
    """HTTPExporter builds a valid TraceIngest body — verified offline."""
    import json
    from unittest.mock import patch

    tracer = LensTracer(service_name="x")
    with tracer.agent("a"):
        pass
    traces = tracer.exporter.traces
    exporter = HTTPExporter("http://127.0.0.1:1", api_key="k")
    captured: dict = {}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"accepted_spans": 1, "traces": ["t"]}'

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.headers.get("X-forge-lens-key")
        captured["body"] = json.loads(req.data)
        return FakeResp()

    with patch("urllib.request.urlopen", fake_urlopen):
        out = exporter.export(traces)
    assert out["accepted_spans"] == 1
    assert captured["url"] == "http://127.0.0.1:1/v1/traces"
    assert captured["key"] == "k"
    assert captured["body"]["schema_version"] == "1.0.0"
    assert captured["body"]["traces"][0]["spans"][0]["name"] == "a"
