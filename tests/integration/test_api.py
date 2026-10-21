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


def test_otlp_malformed_containers_not_500(client: TestClient) -> None:
    """Every unguarded container shape (resource/scope/spans/events/links/
    AnyValue values) used to raise TypeError/AttributeError → HTTP 500.
    All must degrade into 200 + quarantine warnings."""
    span_base = {
        "traceId": "a" * 32, "spanId": "b" * 16, "name": "s",
        "startTimeUnixNano": "1",
    }
    cases = [
        {"resourceSpans": [{"resource": "x", "scopeSpans": [{"spans": [dict(span_base)]}]}]},
        {"resourceSpans": [{"resource": 42, "scopeSpans": [{"spans": [dict(span_base)]}]}]},
        {"resourceSpans": [{"scopeSpans": [{"scope": "x", "spans": [dict(span_base)]}]}]},
        {"resourceSpans": [{"scopeSpans": [
            {"instrumentationLibrary": "x", "spans": [dict(span_base)]}
        ]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": None}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": 5}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [dict(span_base, events=None)]}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [dict(span_base, events=5)]}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [dict(span_base, links=None)]}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [dict(span_base, links=5)]}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [dict(
            span_base, attributes=[{"key": "a", "value": {"arrayValue": {"values": None}}}],
        )]}]}]},
        {"resourceSpans": [{"scopeSpans": [{"spans": [dict(
            span_base, attributes=[{"key": "k", "value": {"kvlistValue": {"values": 5}}}],
        )]}]}]},
    ]
    for payload in cases:
        r = client.post("/v1/otlp/traces", json=payload)
        assert r.status_code in (200, 422), (payload, r.text)


def test_negative_offset_clamped_to_zero(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """offset is clamped like limit — negative input reads as 0, and the
    response echoes the clamped value."""
    envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
    client.post("/v1/traces", json=envelope)
    r = client.get("/v1/traces", params={"offset": -5})
    assert r.status_code == 200
    body = r.json()
    assert body["offset"] == 0
    assert len(body["items"]) == 1


# -- Cycle 03/04 endpoints ---------------------------------------------------


def test_trace_graph_endpoint(client: TestClient, synthetic_trace: Trace) -> None:
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    r = client.get(f"/v1/graph/traces/{synthetic_trace.trace_id}")
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["stats"]["span_count"] == 3
    assert g["root_id"] == "1" * 16
    assert any(e["kind"] == "parent" for e in g["edges"])
    assert any(e["kind"] == "sequence" for e in g["edges"])


def test_critical_path_and_bottlenecks(client: TestClient, synthetic_trace: Trace) -> None:
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    r = client.get(f"/v1/graph/traces/{synthetic_trace.trace_id}/critical-path")
    assert r.status_code == 200
    assert r.json()["path"][0] == "1" * 16
    r = client.get(f"/v1/graph/traces/{synthetic_trace.trace_id}/bottlenecks")
    assert r.status_code == 200
    assert r.json()["items"]


def test_cost_endpoint_priced_and_waste(tmp_path, synthetic_trace: Trace) -> None:
    """pricing_file override flows through the app; waste[] ships in the report."""
    import json as _json

    pricing = tmp_path / "prices.json"
    pricing.write_text(_json.dumps({
        "gpt-test": {"input_per_mtok": 2.5, "output_per_mtok": 10.0}
    }))
    store = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "api.db", pricing_file=pricing
    )
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        c.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
        r = c.get(f"/v1/traces/{synthetic_trace.trace_id}/cost")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["pricing_source"].startswith("file:")
        # 100x2.5 + 42x10.0 per MTok
        assert body["total_estimated_usd"] == round((100 * 2.5 + 42 * 10.0) / 1e6, 8)
        assert "waste" in body
        r = c.get("/v1/analytics/pricing")
        assert r.status_code == 200
        assert "gpt-test" in r.json()["models"]
    store.close()


def test_session_graph_endpoint(client: TestClient, synthetic_trace: Trace) -> None:
    t2 = synthetic_trace.model_copy(deep=True)
    t2.trace_id = "c" * 32
    t2.session_id = "sess-1"
    for s in t2.spans:
        s.trace_id = "c" * 32
    t1 = synthetic_trace.model_copy(update={"session_id": "sess-1"})
    client.post("/v1/traces", json=TraceIngest(traces=[t1, t2]).model_dump(mode="json"))
    r = client.get("/v1/graph/sessions/sess-1")
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["stats"]["trace_count"] == 2
    assert g["edges"][0]["kind"] == "sequence"
    assert g["stats"]["truncated"] is False


# -- round-9 regressions ------------------------------------------------------


def test_otlp_response_includes_sanitize(client: TestClient, otlp_payload: dict) -> None:
    """OTLP ingest returns the same sanitize summary as the native route."""
    r = client.post("/v1/otlp/traces", json=otlp_payload)
    assert r.status_code == 200
    assert "sanitize" in r.json()


def test_auth_reads_gates_get_endpoints(tmp_path) -> None:
    """auth_reads=true + api_key: reads require the key; default stays open."""
    store = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(
        data_dir=tmp_path,
        db_path=tmp_path / "api.db",
        api_key="secret",
        auth_reads=True,
    )
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        assert c.get("/v1/traces").status_code == 401
        assert c.get("/v1/health").status_code == 401
        assert c.get("/v1/metrics").status_code == 401
        assert c.get("/v1/traces", headers={"X-Forge-Lens-Key": "secret"}).status_code == 200
        # ingest still guarded as before
        assert c.post("/v1/traces", json={}).status_code == 401
    store.close()


def test_auth_reads_off_keeps_reads_open(tmp_path) -> None:
    """auth_reads=false (default): GETs stay open even with api_key set."""
    store = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db", api_key="secret")
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        assert c.get("/v1/traces").status_code == 200
        assert c.get("/v1/health").status_code == 200
    store.close()


def test_bad_pricing_file_fails_at_construction(tmp_path) -> None:
    """Operator config errors fail fast at create_app — never as a
    per-request 500 on /cost or /analytics/pricing."""
    bad = tmp_path / "prices.json"
    bad.write_text("{ not json")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "api.db", pricing_file=bad
    )
    with pytest.raises(ValueError):
        create_app(config=cfg)


def test_failure_replay_incident_endpoints(
    client: TestClient, synthetic_trace: Trace
) -> None:
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    tid = synthetic_trace.trace_id

    r = client.get(f"/v1/traces/{tid}/failure")
    assert r.status_code == 200, r.text
    f = r.json()
    assert f["failed"] is True
    # synthetic tool span carries status.message="timeout" — the
    # timeout rule fires before the kind rule (more specific evidence)
    assert f["category"] == "timeout"
    assert f["evidence"]["span_ids"]

    r = client.get(f"/v1/traces/{tid}/replay")
    assert r.status_code == 200
    body = r.json()
    assert body["span_count"] == 3
    assert body["entries"][0]["kind"] == "span_start"
    assert body["entries"][-1]["kind"] == "span_end"
    assert "offset_ms" in body["entries"][0]

    r = client.get(f"/v1/traces/{tid}/incident")
    assert r.status_code == 200
    b = r.json()
    assert b["failure"]["failed"] is True
    assert b["graph"]["stats"]["span_count"] == 3
    assert b["bundle_version"] == "1.0.0"


def test_chunked_oversize_body_rejected(client: TestClient) -> None:
    """No Content-Length (chunked) must not bypass max_payload_bytes."""
    cfg = LensConfig()
    limit = cfg.max_payload_bytes

    def gen():
        yield b" " * 1024 * 1024
        yield b" " * 1024 * 1024  # 2MiB total if limit is 1MiB…
        yield b"x" * limit

    r = client.post(
        "/v1/traces",
        content=gen(),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_otlp_quarantine_warnings_bounded(client: TestClient) -> None:
    """Adversarial malformed-spans payload → capped warnings, not 15x."""
    spans = [{} for _ in range(250)]
    r = client.post(
        "/v1/otlp/traces",
        json={"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]},
    )
    assert r.status_code == 200, r.text
    warnings = r.json()["warnings"]
    assert len(warnings) <= 201
    assert any("more item(s)" in w for w in warnings[-3:])


# -- Cycle 06: evaluation endpoints --------------------------------------------


def test_trace_eval_endpoint(client: TestClient, synthetic_trace: Trace) -> None:
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    r = client.get(f"/v1/traces/{synthetic_trace.trace_id}/eval")
    assert r.status_code == 200, r.text
    results = {res["evaluator"]: res for res in r.json()["results"]}
    assert results["error_free"]["score"] == 0.0  # fixture has an error span
    assert results["tool_success"]["score"] == 0.0


def test_eval_run_and_compare_endpoints(
    client: TestClient, synthetic_trace: Trace
) -> None:
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    tid = synthetic_trace.trace_id
    r = client.post(
        "/v1/eval/run",
        json={"trace_ids": [tid], "evaluators": [{"name": "error_free"}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["summary"]["error_free/no_error_spans"]["n"] == 1

    r = client.post(
        "/v1/eval/compare",
        json={
            "a": {"trace_ids": [tid]},
            "b": {"trace_ids": [tid]},
            "evaluators": [{"name": "error_free"}],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["deltas"][0]["delta"] == 0.0

    # malformed bodies → 4xx, never 500
    assert client.post("/v1/eval/run", json={"evaluators": []}).status_code == 422
    assert client.post(
        "/v1/eval/compare", json={"a": {}, "b": {"trace_ids": [tid]},
                                  "evaluators": [{"name": "x"}]}
    ).status_code == 422


def test_eval_malformed_bodies_are_4xx_not_500(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """Every malformed eval body shape must be 4xx — the round-3 rule."""
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    tid = synthetic_trace.trace_id
    bad_bodies = [
        ("{not json", "raw"),                                    # bad JSON
        ('[1,2]', "raw"),                                        # non-object
        ({"trace_ids": [tid], "evaluators": []}, "json"),        # empty specs
        ({"trace_ids": [tid] * 501,
          "evaluators": [{"name": "error_free"}]}, "json"),      # over cap
        ({"trace_ids": [""], "evaluators": []}, "json"),         # empty specs 2
        ({"trace_ids": [1, 2], "evaluators": [{"name": "x"}]}, "json"),  # non-str ids
        ({"trace_ids": [tid], "evaluators": [{"name": "error_free"}] * 101}, "json"),
        ({"trace_ids": [tid], "evaluators": [{"name": "error_free"}],
          "dataset": 123}, "json"),                              # non-str dataset
        ({"trace_ids": [tid], "evaluators": [{"name": "error_free"}],
          "project_id": {"x": 1}}, "json"),                      # non-str project
    ]
    for body, kind in bad_bodies:
        if kind == "raw":
            r = client.post("/v1/eval/run", content=body,
                            headers={"Content-Type": "application/json"})
        else:
            r = client.post("/v1/eval/run", json=body)
        assert r.status_code in (400, 404, 422), (body, r.status_code, r.text)

    # malformed compare bodies
    for body in (
        "{bad",
        {"a": {}, "b": {"trace_ids": [tid]}, "evaluators": [{"name": "x"}]},
        {"a": {"trace_ids": []}, "b": {"trace_ids": [tid]},
         "evaluators": [{"name": "x"}]},                          # empty side
        {"a": {"trace_ids": [tid]}, "b": "x",
         "evaluators": [{"name": "x"}]},
        {"a": {"trace_ids": [tid]}, "b": {"trace_ids": [tid]},
         "evaluators": [{"name": "x"}], "project_id": 5},
    ):
        r = client.post(
            "/v1/eval/compare",
            content=body if isinstance(body, str) else None,
            json=None if isinstance(body, str) else body,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code in (400, 404, 422), (body, r.status_code)


def test_eval_specs_never_500_even_when_junk(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """Junk spec *contents* (non-dict, str params) score None — the
    engine reports the defect instead of crashing on it."""
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    tid = synthetic_trace.trace_id
    r = client.post(
        "/v1/eval/run",
        json={"trace_ids": [tid],
              "evaluators": [{"name": "has_span", "params": "oops"}, 42]},
    )
    assert r.status_code == 200
    results = r.json()["results"][tid]
    assert all(res["score"] is None for res in results)
    assert any("spec must be an object" in res["details"]["reason"]
               for res in results)
    assert any("params must be an object" in res["details"]["reason"]
               for res in results)


def test_eval_surrogate_strings_are_422_not_500(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """WTF-8 bodies: json.loads accepts lone surrogates; binding them
    into sqlite raises UnicodeEncodeError — now a clean 422."""
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    tid = synthetic_trace.trace_id
    # a \\ud800 escape is VALID JSON — json.loads materializes a lone
    # surrogate that isinstance(str) accepts but sqlite can't bind
    for raw in (
        '{"trace_ids": ["\\ud800"], "evaluators": [{"name": "error_free"}]}',
        f'{{"trace_ids": ["{tid}"], "project_id": "\\ud800", '
        '"evaluators": [{"name": "error_free"}]}',
        f'{{"a": {{"trace_ids": ["{tid}"]}}, "b": {{"trace_ids": ["{tid}"], '
        '"project_id": "\\ud800"}, '
        '"evaluators": [{"name": "error_free"}]}',
    ):
        url = "/v1/eval/compare" if raw.startswith('{"a"') else "/v1/eval/run"
        r = client.post(url, content=raw,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 422, (raw[:60], r.status_code, r.text)


def test_eval_non_finite_params_score_none_not_verdict(
    client: TestClient, synthetic_trace: Trace
) -> None:
    """Infinity/NaN budgets must not fabricate a confident pass/fail —
    they degrade to score=None like every other bad spec."""
    client.post("/v1/traces", json=TraceIngest(traces=[synthetic_trace]).model_dump(mode="json"))
    tid = synthetic_trace.trace_id
    # Python's json.loads accepts Infinity/NaN literals — real clients
    # send them as raw text; strict serializers can't even emit them
    for budget in ("Infinity", "-Infinity", "NaN", "1e999"):
        raw = (
            f'{{"trace_ids": ["{tid}"], "evaluators": [{{"name": '
            f'"latency_budget", "params": {{"budget_ms": {budget}}}}}]}}'
        )
        r = client.post("/v1/eval/run", content=raw,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 200, (budget, r.text)
        res = r.json()["results"][tid][0]
        assert res["score"] is None, budget
    for budget in ("Infinity", "NaN"):
        raw = (
            f'{{"trace_ids": ["{tid}"], "evaluators": [{{"name": '
            f'"token_budget", "params": {{"max_total_tokens": {budget}}}}}]}}'
        )
        r = client.post("/v1/eval/run", content=raw,
                        headers={"Content-Type": "application/json"})
        assert r.status_code == 200, (budget, r.text)
        assert r.json()["results"][tid][0]["score"] is None
