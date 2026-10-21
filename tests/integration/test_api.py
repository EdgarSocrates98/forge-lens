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


def test_native_payload_size_limit(tmp_path, synthetic_trace: Trace) -> None:
    """POST /v1/traces enforces max_payload_bytes — not just OTLP."""
    store = SQLiteTraceStore(tmp_path / "small.db")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "small.db", max_payload_bytes=1024
    )
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
        r = c.post("/v1/traces", json=envelope)
        assert r.status_code == 413
    store.close()


def test_otlp_payload_size_limit(tmp_path, otlp_payload: dict) -> None:
    store = SQLiteTraceStore(tmp_path / "small.db")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "small.db", max_payload_bytes=256
    )
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        r = c.post("/v1/otlp/traces", json=otlp_payload)
        assert r.status_code == 413
    store.close()


def test_span_trace_id_mismatch_is_422(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """Span declaring another trace_id → controlled 422, nothing stored."""
    bad = synthetic_trace.model_copy(deep=True)
    bad.spans[1].trace_id = "d" * 32
    envelope = TraceIngest(traces=[bad]).model_dump(mode="json")
    r = client.post("/v1/traces", json=envelope)
    assert r.status_code == 422
    assert client.get(f"/v1/traces/{bad.trace_id}").status_code == 404


def test_otlp_malformed_span_quarantined_not_500(
    client: TestClient, otlp_payload: dict
) -> None:
    """One bad span → 200 + quarantine warning, good spans still land."""
    otlp_payload["resourceSpans"][0]["scopeSpans"][0]["spans"].append(
        {
            "traceId": "b" * 32,
            "spanId": "f" * 16,
            "name": "broken",
            "startTimeUnixNano": "notnano",
        }
    )
    r = client.post("/v1/otlp/traces", json=otlp_payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted_spans"] == 2
    assert any("quarantined" in w for w in body["warnings"])

    # the trace itself is retrievable
    r = client.get(f"/v1/traces/{'b' * 32}")
    assert r.status_code == 200


def test_otlp_bad_intvalue_quarantined_not_500(
    client: TestClient, otlp_payload: dict
) -> None:
    otlp_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"].append(
        {"key": "n", "value": {"intValue": "junk"}}
    )
    r = client.post("/v1/otlp/traces", json=otlp_payload)
    assert r.status_code == 200, r.text
    body = r.json()
    # the polluted span was quarantined; its sibling survived
    assert body["accepted_spans"] == 1
    assert any("intValue" in w for w in body["warnings"])


def test_native_ingest_reports_updated_spans(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """Late end_time via API → updated_spans, stored span completes."""
    running = synthetic_trace.model_copy(deep=True)
    running.spans = [running.spans[0]]
    running.spans[0].end_time = None
    envelope = TraceIngest(traces=[running]).model_dump(mode="json")
    r = client.post("/v1/traces", json=envelope)
    assert r.status_code == 200
    assert r.json()["accepted_spans"] == 1

    completed = synthetic_trace.model_copy(deep=True)
    completed.spans = [completed.spans[0]]
    envelope = TraceIngest(traces=[completed]).model_dump(mode="json")
    r = client.post("/v1/traces", json=envelope)
    assert r.status_code == 200
    body = r.json()
    assert body["updated_spans"] == 1
    assert body["duplicate_spans"] == 0

    stored = client.get(f"/v1/traces/{synthetic_trace.trace_id}").json()
    assert stored["spans"][0]["end_time"] is not None


def test_envelope_project_id_fills_default_traces(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """A non-default envelope project applies to traces that didn't
    name a project of their own."""
    envelope = TraceIngest(traces=[synthetic_trace], project_id="proj-a")
    r = client.post("/v1/traces", json=envelope.model_dump(mode="json"))
    assert r.status_code == 200, r.text
    stored = client.get(
        f"/v1/traces/{synthetic_trace.trace_id}", params={"project_id": "proj-a"}
    ).json()
    assert stored["project_id"] == "proj-a"


def test_default_envelope_preserves_explicit_trace_project(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """A per-trace project_id is authoritative: envelope 'default' must
    not clobber it (that would silently misplace the trace)."""
    own = synthetic_trace.model_copy(deep=True)
    own.project_id = "proj-claimed"
    envelope = TraceIngest(traces=[own])  # envelope project stays 'default'
    r = client.post("/v1/traces", json=envelope.model_dump(mode="json"))
    assert r.status_code == 200, r.text
    stored = client.get(
        f"/v1/traces/{synthetic_trace.trace_id}",
        params={"project_id": "proj-claimed"},
    ).json()
    assert stored["project_id"] == "proj-claimed"


def test_otlp_non_object_body_returns_422(client: TestClient) -> None:
    """Valid JSON but wrong top-level type → 4xx, never a 500."""
    for bad in ("[1,2,3]", '"hello"', "42", "null"):
        r = client.post(
            "/v1/otlp/traces", content=bad,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 422, (bad, r.status_code, r.text)


def test_native_mixed_naive_aware_times_not_500(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """A schema-valid trace mixing naive and aware timestamps must not
    crash flag_integrity — naive is read as UTC at the boundary."""
    mixed = synthetic_trace.model_copy(deep=True)
    # span 1 keeps aware times; span 2 gets a naive end_time that
    # compares against its aware start_time → TypeError without
    # boundary normalization
    end = mixed.spans[1].end_time
    assert end is not None
    mixed.spans[1].end_time = end.replace(tzinfo=None)
    envelope = TraceIngest(traces=[mixed]).model_dump(mode="json")
    r = client.post("/v1/traces", json=envelope)
    assert r.status_code == 200, r.text

    stored = client.get(f"/v1/traces/{mixed.trace_id}").json()
    # naive end was coerced to UTC-aware, not dropped
    assert stored["spans"][1]["end_time"].endswith("Z") or "+" in stored["spans"][1]["end_time"]


def test_native_naive_sent_at_not_500(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """Envelope sent_at naive vs aware trace.started_at — same hazard,
    same fix path."""
    envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
    envelope["sent_at"] = "2026-01-01T11:59:00"  # naive, predates start
    r = client.post("/v1/traces", json=envelope)
    assert r.status_code == 200, r.text
    assert any("sent_at" in w for w in r.json()["warnings"])


def test_max_traces_per_request_limit(
    tmp_path, synthetic_trace: Trace
) -> None:
    """An under-byte-limit payload carrying N empty traces is bounded."""
    store = SQLiteTraceStore(tmp_path / "many.db")
    cfg = LensConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "many.db",
        max_traces_per_request=5,
    )
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        empty = synthetic_trace.model_copy(deep=True)
        empty.spans = []
        traces = [
            empty.model_copy(update={"trace_id": f"{i:032x}"}, deep=True)
            for i in range(10)
        ]
        r = c.post(
            "/v1/traces",
            json=TraceIngest(traces=traces).model_dump(mode="json"),
        )
        assert r.status_code == 413
    store.close()


def test_import_has_no_db_side_effect(tmp_path, monkeypatch) -> None:
    """importing api.app must not create .forge-lens/ or open a DB."""
    import importlib
    import sys

    monkeypatch.chdir(tmp_path)
    for mod in list(sys.modules):
        if mod.startswith("forge_lens.api"):
            del sys.modules[mod]
    importlib.import_module("forge_lens.api.app")
    assert not (tmp_path / ".forge-lens").exists()


def test_otlp_protobuf_content_type_rejected_415(client: TestClient) -> None:
    """The docstring claims explicit protobuf rejection — it is real."""
    r = client.post(
        "/v1/otlp/traces",
        content=b"\x0a\x00",
        headers={"Content-Type": "application/x-protobuf"},
    )
    assert r.status_code == 415


def test_ingest_rejections_count_as_ingest_errors(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """413/422 rejections must show up in lens.ingest.errors, not only
    in lens.api.errors — metric accounting is consistent."""
    from forge_lens.core.metrics import METRICS

    before = METRICS.snapshot()["counters"].get("lens.ingest.errors", 0)
    client.post(
        "/v1/otlp/traces", content=b"[1,2]",
        headers={"Content-Type": "application/json"},
    )
    after = METRICS.snapshot()["counters"].get("lens.ingest.errors", 0)
    assert after == before + 1


def test_otlp_deeply_nested_anyvalue_not_500(client: TestClient) -> None:
    """A ~2000-deep nested AnyValue used to RecursionError → HTTP 500.
    The depth cap makes it a bounded parse with an explicit marker."""
    deep: dict = {"stringValue": "bottom"}
    for _ in range(2_000):
        deep = {"arrayValue": {"values": [deep]}}
    r = client.post(
        "/v1/otlp/traces",
        json={
            "resourceSpans": [{"scopeSpans": [{"spans": [{
                "traceId": "a" * 32, "spanId": "b" * 16, "name": "s",
                "startTimeUnixNano": "1", "endTimeUnixNano": "2",
                "attributes": [{"key": "deep", "value": deep}],
            }]}]}]
        },
    )
    assert r.status_code == 200
    assert r.json()["accepted_spans"] == 1


def test_oversized_content_length_rejected_before_read(
    client: TestClient,
) -> None:
    """Content-Length beyond the cap rejects without buffering the body."""
    r = client.post(
        "/v1/traces",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(999 * 1024 * 1024),
        },
    )
    assert r.status_code == 413


def test_auth_non_ascii_header_401_not_500(
    tmp_path, synthetic_trace: Trace
) -> None:
    """secrets.compare_digest raises TypeError on non-ASCII str input —
    an attacker-controlled header must get 401, not an unhandled 500."""
    store = SQLiteTraceStore(tmp_path / "auth-nonascii.db")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "auth-nonascii.db", api_key="s3cret"
    )
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
        # httpx ASCII-encodes str headers — a raw client sends bytes,
        # which Starlette decodes latin-1 into a non-ASCII str
        r = c.post(
            "/v1/traces",
            json=envelope,
            headers={b"X-Forge-Lens-Key": "clé-secrète".encode()},
        )
        assert r.status_code == 401
        # and a correctly-formed key still works
        r = c.post(
            "/v1/traces",
            json=envelope,
            headers={"X-Forge-Lens-Key": "s3cret"},
        )
        assert r.status_code == 200
    store.close()
