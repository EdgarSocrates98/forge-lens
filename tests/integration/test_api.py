"""API tests via httpx TestClient — no real sockets."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forge_lens import __version__
from forge_lens.api.app import create_app
from forge_lens.contracts.v1.trace import Trace, TraceIngest
from forge_lens.core.config import LensConfig
from forge_lens.storage.sqlite import SQLiteTraceStore


@pytest.fixture
def client(tmp_path, synthetic_trace: Trace):
    store = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db")
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        c._lens_store = store  # type: ignore[attr-defined]
        c._lens_trace = synthetic_trace  # type: ignore[attr-defined]
        yield c
    store.close()


def test_health(client: TestClient) -> None:
    r = client.get("/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert body["storage"] == "sqlite"


def test_native_ingest_and_retrieve(client: TestClient, synthetic_trace: Trace) -> None:
    envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
    r = client.post("/v1/traces", json=envelope)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted_spans"] == 3

    r = client.get(f"/v1/traces/{synthetic_trace.trace_id}")
    assert r.status_code == 200
    trace = r.json()
    assert len(trace["spans"]) == 3

    r = client.get("/v1/traces", params={"status": "error"})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1


def test_otlp_ingest(client: TestClient, otlp_payload: dict) -> None:
    r = client.post("/v1/otlp/traces", json=otlp_payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted_spans"] == 2
    assert body["traces"] == ["b" * 32]


def test_missing_trace_404(client: TestClient) -> None:
    assert client.get(f"/v1/traces/{'f' * 32}").status_code == 404


def test_auth_required_when_key_configured(tmp_path, synthetic_trace: Trace) -> None:
    store = SQLiteTraceStore(tmp_path / "auth.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "auth.db", api_key="s3cret")
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
        assert c.post("/v1/traces", json=envelope).status_code == 401
        assert c.post(
            "/v1/traces", json=envelope, headers={"X-Forge-Lens-Key": "wrong"}
        ).status_code == 401
        assert c.post(
            "/v1/traces", json=envelope, headers={"X-Forge-Lens-Key": "s3cret"}
        ).status_code == 200
        # queries stay open (local-first reads)
        assert c.get("/v1/health").status_code == 200
    store.close()


def test_metrics_endpoint(client: TestClient, synthetic_trace: Trace) -> None:
    envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
    client.post("/v1/traces", json=envelope)
    r = client.get("/v1/metrics")
    assert r.status_code == 200
    assert r.json()["counters"]["lens.ingest.spans"] >= 3


def test_otlp_invalid_json(client: TestClient) -> None:
    r = client.post(
        "/v1/otlp/traces", content=b"{broken", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 400
