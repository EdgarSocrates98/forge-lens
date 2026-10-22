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


def _otlp_body_with_deep_attr(nesting: int) -> bytes:
    """OTLP envelope + one ``deep`` attribute nested ``nesting`` levels —
    built as raw bytes so the fixture itself never recurses (the
    RecursionError the CI saw on Windows was the *fixture's* json.dumps,
    not the server)."""
    deep = (
        b'{"arrayValue":{"values":[' * nesting
        + b'{"stringValue":"bottom"}'
        + b']}}' * nesting
    )
    return (
        b'{"resourceSpans":[{"scopeSpans":[{"spans":[{'
        b'"traceId":"' + b"a" * 32 + b'",'
        b'"spanId":"' + b"b" * 16 + b'",'
        b'"name":"s","startTimeUnixNano":"1","endTimeUnixNano":"2",'
        b'"attributes":[{"key":"deep","value":' + deep + b"}]"
        b"}]}]}]}"
    )


def test_otlp_deeply_nested_anyvalue_not_500(client: TestClient) -> None:
    """A ~2000-deep nested AnyValue is over the transport depth bound —
    deterministic 400 on every platform, never a RecursionError-500."""
    r = client.post(
        "/v1/otlp/traces",
        content=_otlp_body_with_deep_attr(2_000),
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert "nesting" in r.json()["detail"]


def test_otlp_nested_anyvalue_within_limit_accepted(
    client: TestClient,
) -> None:
    """Depth within the transport bound parses fine; the semantic
    AnyValue cap (32) still marks deep content ``[DROPPED:depth-limit]``."""
    r = client.post(
        "/v1/otlp/traces",
        content=_otlp_body_with_deep_attr(100),
        headers={"Content-Type": "application/json"},
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


def test_eval_compare_validates_both_sides_before_fanout(
    client: TestClient,
) -> None:
    """A malformed `b` must 422 — never be masked by `a`'s store lookups
    (here `a` names a missing trace, which would previously 404 first)."""
    missing = "f" * 32
    r = client.post(
        "/v1/eval/compare",
        json={
            "a": {"trace_ids": [missing]},
            "b": "not-an-object",
            "evaluators": [{"name": "error_free"}],
        },
    )
    assert r.status_code == 422
    assert "b" in r.json()["detail"]


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


def test_eval_boundary_invalid_utf8_is_400_not_500(client: TestClient) -> None:
    """json.loads(bytes) raises UnicodeDecodeError on invalid UTF-8 —
    a distinct exception class from JSONDecodeError, and the same 400
    reject class at every body-parsing boundary."""
    for url in ("/v1/eval/run", "/v1/eval/compare"):
        for body in (b'{"x": "\xff"}', b'{"x": "\xc3\x28"}'):
            r = client.post(
                url, content=body,
                headers={"Content-Type": "application/json"},
            )
            assert r.status_code == 400, (url, body, r.status_code, r.text)


def test_eval_boundary_deep_nesting_is_400_not_500(client: TestClient) -> None:
    """Deeply-nested *valid* JSON is over the documented depth bound —
    deterministic 400 on every platform (the lexical check runs before
    any parser, so no platform's recursion limit is in play)."""
    body = b"[" * 80_000 + b"]" * 80_000  # ~160KB, under the byte cap
    for url in ("/v1/eval/run", "/v1/eval/compare"):
        r = client.post(
            url, content=body,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400, (url, r.status_code, r.text)
        assert "nesting" in r.json()["detail"]


def test_json_depth_boundary_is_consistent(client: TestClient) -> None:
    """Depth exactly at the bound parses; one level over → 400 — on all
    platforms, since the check is lexical, not parser recursion."""
    at = b"[" * 512 + b"]" * 512          # exactly _MAX_JSON_DEPTH
    over = b"[" * 513 + b"]" * 513
    r = client.post(
        "/v1/eval/run", content=at,
        headers={"Content-Type": "application/json"},
    )
    # parses fine → reaches the body-shape check → 422 (a list ≠ object)
    assert r.status_code == 422
    r = client.post(
        "/v1/eval/run", content=over,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    # ingest paths reject with the same class
    r = client.post(
        "/v1/otlp/traces", content=over,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_eval_run_validates_dataset_before_trace_lookup(
    client: TestClient,
) -> None:
    """Cheap-validation-first applies to /eval/run too: a malformed
    `dataset` must 422 before any trace fetch can 404."""
    r = client.post(
        "/v1/eval/run",
        json={
            "trace_ids": ["f" * 32],  # missing — would 404 if reached
            "evaluators": [{"name": "error_free"}],
            "dataset": 123,
        },
    )
    assert r.status_code == 422
    assert "dataset" in r.json()["detail"]

    # compare errors name their side — "b.dataset", not a bare field
    r = client.post(
        "/v1/eval/compare",
        json={
            "a": {"trace_ids": ["f" * 32]},
            "b": {"trace_ids": ["f" * 32], "dataset": 5},
            "evaluators": [{"name": "error_free"}],
        },
    )
    assert r.status_code == 422
    assert "b.dataset" in r.json()["detail"]


def test_eval_boundary_oversized_int_is_400_not_500(client: TestClient) -> None:
    """json.loads raises a plain ValueError on int literals over
    set_int_max_str_digits — same 400 class as every other parse
    failure, never a 500."""
    import sys

    body = b'{"n": ' + b"9" * (sys.get_int_max_str_digits() + 100) + b"}"
    for url in ("/v1/eval/run", "/v1/eval/compare"):
        r = client.post(
            url, content=body,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400, (url, r.status_code, r.text)


def test_datasets_api(client: TestClient) -> None:
    r = client.put("/v1/datasets/qa", json={"name": "QA set", "task": "qa"})
    assert r.status_code == 200, r.text
    r = client.get("/v1/datasets/qa")
    assert r.status_code == 200
    assert r.json()["name"] == "QA set"

    r = client.put("/v1/datasets/qa/versions/v1", json={
        "description": "first",
        "cases": [
            {"case_id": "c1", "expected": {"answer": "42"},
             "allowed_tools": ["web_search"]},
        ],
    })
    assert r.status_code == 200
    assert r.json()["cases"] == 1

    r = client.get("/v1/datasets/qa/versions")
    assert r.status_code == 200
    assert r.json()[0]["version"] == "v1"
    assert r.json()[0]["case_count"] == 1

    r = client.get("/v1/datasets/qa/versions/v1")
    assert r.status_code == 200
    assert r.json()["cases"][0]["case_id"] == "c1"

    # boundary shapes
    assert client.put("/v1/datasets/bad", json={}).status_code == 422
    assert client.put(
        "/v1/datasets/missing/versions/v1", json={"cases": []}
    ).status_code == 404
    assert client.get("/v1/datasets/qa/versions/v9").status_code == 404
    assert client.get("/v1/datasets/nope").status_code == 404
    r = client.get("/v1/datasets")
    assert r.status_code == 200
    assert any(d["dataset_id"] == "qa" for d in r.json())


def test_eval_new_evaluators_and_gate(client: TestClient, synthetic_trace: Trace) -> None:
    envelope = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
    assert client.post("/v1/traces", json=envelope).status_code == 200

    r = client.post("/v1/eval/run", json={
        "trace_ids": [synthetic_trace.trace_id],
        "evaluators": [
            {"name": "exact_match",
             "params": {"field": "llm.model_name", "expected": "gpt-test"}},
            {"name": "token_f1",
             "params": {"field": "tool.name", "expected": "web search"}},
            {"name": "allowed_tools", "params": {"allowed": ["web_search"]}},
            {"name": "allowed_tools", "params": {"allowed": ["other"]}},
            {"name": "task_completion", "params": {"require_no_error": True}},
        ],
    })
    assert r.status_code == 200, r.text
    results = r.json()["results"][synthetic_trace.trace_id]
    scores = [res["score"] for res in results]
    assert scores[0] == 1.0                      # exact_match
    assert scores[1] == 1.0                      # token_f1 (bag-of-tokens)
    assert scores[2] == 1.0                      # allowed_tools pass
    assert scores[3] == 0.0                      # allowed_tools violation
    assert "web_search" in results[3]["details"]["violations"]
    assert scores[4] == 0.0                      # task_completion: error span

    # gate: pass when floor is met, fail when it isn't — always with evidence
    body = {
        "a": {"trace_ids": [synthetic_trace.trace_id]},
        "b": {"trace_ids": [synthetic_trace.trace_id]},
        "evaluators": [{"name": "error_free"}],
    }
    r = client.post("/v1/eval/compare", json={
        **body, "gate": {"metrics": {
            "error_free/no_error_spans": {"min_pass_rate_b": 0.0}}}}
    )
    assert r.status_code == 200
    assert r.json()["gate"]["gate_passed"] is True
    r = client.post("/v1/eval/compare", json={
        **body, "gate": {"metrics": {
            "error_free/no_error_spans": {"min_pass_rate_b": 1.0}}}}
    )
    gate = r.json()["gate"]
    assert gate["gate_passed"] is False
    assert gate["failures"][0]["observed"] == 0.0
    # gating on an unevaluated metric is itself a failure
    r = client.post("/v1/eval/compare", json={
        **body, "gate": {"metrics": {"nope/metric": {}}}}
    )
    assert r.json()["gate"]["gate_passed"] is False
    assert client.post("/v1/eval/compare", json={
        **body, "gate": "x"}).status_code == 422


def test_keys_mode_tenancy(tmp_path, synthetic_trace: Trace) -> None:
    """Cycle 08 E2E: managed keys, roles, project isolation, audit."""
    from forge_lens.security.keys import generate_key

    store = SQLiteTraceStore(tmp_path / "k.db")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "k.db", auth_mode="keys"
    )
    app = create_app(store=store, config=cfg)
    kid, secret, h = generate_key()
    store.create_api_key(kid, name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": secret}
            # unauthenticated → 401 even on reads (isolation requires auth)
            assert c.get("/v1/traces").status_code == 401
            assert c.get("/v1/health").status_code == 401
            # admin works
            assert c.get("/v1/health", headers=admin).status_code == 200
            # mint a p1-scoped writer through the API
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "w", "role": "writer",
                             "projects": ["p1"]})
            assert r.status_code == 200, r.text
            scoped = {"X-Forge-Lens-Key": r.json()["secret"]}
            # writer ingests into its tenant
            env = TraceIngest(
                traces=[synthetic_trace], project_id="p1"
            ).model_dump(mode="json")
            r = c.post("/v1/traces", json=env, headers=scoped)
            assert r.status_code == 200, r.text
            # but not into the default tenant
            env2 = TraceIngest(traces=[synthetic_trace]).model_dump(mode="json")
            assert c.post("/v1/traces", json=env2, headers=scoped).status_code == 403
            # scoped read inside tenant ok, outside denied
            assert c.get("/v1/traces?project_id=p1", headers=scoped).status_code == 200
            assert c.get("/v1/traces?project_id=default", headers=scoped).status_code == 403
            # reader role cannot write
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "ro", "role": "reader"})
            reader = {"X-Forge-Lens-Key": r.json()["secret"]}
            assert c.post("/v1/traces", json=env, headers=reader).status_code == 403
            # non-admin cannot reach admin routes
            assert c.get("/v1/admin/keys", headers=reader).status_code == 403
            assert c.get("/v1/admin/keys").status_code == 401
            # audit: denials + key.create recorded
            events = c.get("/v1/admin/audit", headers=admin).json()
            actions = {e["action"] for e in events}
            assert {"auth", "key.create", "scope"} <= actions
    finally:
        store.close()


def test_keys_mode_tenancy_all_new_routes(tmp_path, synthetic_trace: Trace) -> None:
    """P0 regression: every new-surface route enforces project scope —
    a p1-scoped reader must never see p2 data."""
    from forge_lens.security.keys import generate_key

    store = SQLiteTraceStore(tmp_path / "k2.db")
    # victim data lives in p2
    victim = synthetic_trace.model_copy(update={"project_id": "p2"})
    store.upsert_trace(victim)
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "k2.db", auth_mode="keys"
    )
    app = create_app(store=store, config=cfg)
    kid, secret, h = generate_key()
    store.create_api_key(kid, name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": secret}
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "p1-reader", "role": "reader",
                             "projects": ["p1"]})
            scoped = {"X-Forge-Lens-Key": r.json()["secret"]}
            tid = synthetic_trace.trace_id
            gets = [
                f"/v1/traces/{tid}?project_id=p2",
                f"/v1/traces/{tid}/stream?project_id=p2",
                f"/v1/traces/{tid}/loops?project_id=p2",
                f"/v1/traces/{tid}/efficiency?project_id=p2",
                f"/v1/traces/{tid}/recommendations?project_id=p2",
                f"/v1/traces/{tid}/eval?project_id=p2",
                f"/v1/traces/{tid}/cost?project_id=p2",
                f"/v1/traces/{tid}/failure?project_id=p2",
                f"/v1/traces/{tid}/replay?project_id=p2",
                f"/v1/traces/{tid}/incident?project_id=p2",
                f"/v1/traces/{tid}/security?project_id=p2",
                f"/v1/graph/traces/{tid}?project_id=p2",
                f"/v1/graph/traces/{tid}/critical-path?project_id=p2",
                f"/v1/graph/traces/{tid}/bottlenecks?project_id=p2",
                f"/v1/graph/traces/{tid}/analysis?project_id=p2",
                "/v1/graph/sessions/s1?project_id=p2",
                "/v1/ops/summary?project_id=p2",
                "/v1/sessions?project_id=p2",
                "/v1/tools?project_id=p2",
            ]
            for url in gets:
                r = c.get(url, headers=scoped)
                assert r.status_code == 403, f"{url} → {r.status_code}"
            posts = [
                (f"/v1/analyze/loop_detection_specialist/{tid}",
                 {"project_id": "p2"}),
                ("/v1/intel/efficiency",
                 {"project_id": "p2", "trace_ids": [tid]}),
                ("/v1/intel/regress",
                 {"a": {"trace_ids": [tid], "project_id": "p2"},
                  "b": {"trace_ids": [tid], "project_id": "p1"}}),
                ("/v1/alerts/evaluate",
                 {"project_id": "p2",
                  "rules": [{"rule_id": "x", "metric": "trace_count",
                             "op": ">", "threshold": -1}]}),
                ("/v1/eval/run",
                 {"project_id": "p2", "trace_ids": [tid],
                  "evaluators": [{"name": "error_free"}]}),
            ]
            for url, body in posts:
                r = c.post(url, json=body, headers=scoped)
                assert r.status_code == 403, f"{url} → {r.status_code}: {r.text}"
            # own-project reads still work
            assert c.get("/v1/ops/summary?project_id=p1",
                         headers=scoped).status_code == 200
    finally:
        store.close()


def test_intel_and_analyze_malformed_422_not_500(client: TestClient) -> None:
    """Boundary hardening on the new POST routes — same 4xx contract."""
    assert client.post("/v1/intel/efficiency", json={
        "trace_ids": "notalist"}).status_code == 422
    assert client.post("/v1/intel/efficiency", json={
        "project_id": {"x": 1}}).status_code == 422
    assert client.post("/v1/intel/efficiency", json={
        "trace_ids": [1, 2]}).status_code == 422
    for bad in (
        {"budget": {"max_iterations": "abc"}},
        {"budget": {"max_iterations": True}},
        {"budget": {"token_budget": [1]}},
        {"budget": {"timeout_s": {"x": 1}}},
        {"budget": "no"},
        {"context": "no"},
        {"project_id": {"x": 1}},
    ):
        r = client.post("/v1/analyze/trace_investigator/" + "ab" * 16,
                        json=bad)
        assert r.status_code == 422, f"{bad} → {r.status_code}"
    r = client.post("/v1/intel/regress", json={"a": "no", "b": {}})
    assert r.status_code == 422


def test_tenancy_omitted_project_id_paths(tmp_path, synthetic_trace: Trace) -> None:
    """P0: the omitted-project_id path must not widen into a global read.
    Scoped key naming no project resolves to cfg.project_id — and gets
    403 if its scope doesn't cover that, or default-only data if it does."""
    from forge_lens.security.keys import generate_key

    store = SQLiteTraceStore(tmp_path / "k3.db")
    victim = synthetic_trace.model_copy(update={"project_id": "secret"})
    store.upsert_trace(victim)
    decoy = synthetic_trace.model_copy(update={"project_id": "default"})
    store.upsert_trace(decoy)
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "k3.db", auth_mode="keys"
    )
    app = create_app(store=store, config=cfg)
    kid, secret, h = generate_key()
    store.create_api_key(kid, name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": secret}
            # key scoped to p1 only — omitted pid resolves to 'default',
            # outside its scope → 403 (never a silent global read)
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "p1", "role": "reader",
                             "projects": ["p1"]})
            p1 = {"X-Forge-Lens-Key": r.json()["secret"]}
            assert c.get("/v1/ops/summary", headers=p1).status_code == 403
            assert c.get("/v1/datasets", headers=p1).status_code == 403
            assert c.post("/v1/alerts/evaluate", json={}, headers=p1
                          ).status_code == 403
            assert c.post("/v1/intel/efficiency", json={}, headers=p1
                          ).status_code == 403
            # key scoped to 'default' — omitted pid reads ONLY default
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "d", "role": "reader",
                             "projects": ["default"]})
            dk = {"X-Forge-Lens-Key": r.json()["secret"]}
            s = c.get("/v1/ops/summary", headers=dk)
            assert s.status_code == 200
            assert s.json()["stats"]["traces"] == 1  # decoy only, not 'secret'
            r = c.post("/v1/alerts/evaluate", headers=dk,
                       json={"use_saved": False, "rules": [
                           {"rule_id": "x", "metric": "trace_count",
                            "op": ">", "threshold": 0}]})
            assert r.status_code == 200
            assert r.json()["fired"][0]["observed"] == 1.0
            assert c.get("/v1/datasets", headers=dk).status_code == 200
            assert c.get("/v1/health", headers=dk).json()["stats"]["traces"] == 1
    finally:
        store.close()


def test_analyze_bad_context_422_and_alert_rules_io(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    env = {"traces": [synthetic_trace.model_dump(mode="json")]}
    assert client.post("/v1/traces", json=env).status_code == 200
    tid = synthetic_trace.trace_id
    r = client.post(
        f"/v1/analyze/loop_detection_specialist/{tid}",
        json={"context": {"repeat_threshold": "abc"}},
    )
    assert r.status_code == 422
    r = client.post(f"/v1/analyze/trace_investigator/{tid}",
                    json={"run_id": {"x": 1}})
    assert r.status_code == 422
    # saved rules round-trip via API (default auth mode → admin unscoped)
    r = client.post("/v1/alerts/rules", json={"rule": {
        "rule_id": "persisted", "metric": "trace_count",
        "op": ">=", "threshold": 0}})
    assert r.status_code == 200
    r = client.post("/v1/alerts/evaluate", json={"use_saved": True})
    assert r.status_code == 200
    assert {f["rule_id"] for f in r.json()["fired"]} == {"persisted"}
    assert client.delete("/v1/alerts/rules/persisted").status_code == 200
    assert client.delete("/v1/alerts/rules/persisted").status_code == 404


def test_cross_tenant_write_rejected(tmp_path, synthetic_trace: Trace) -> None:
    """P0: a writer key scoped to p1 must not persist a trace whose
    own project_id claims p2 — the per-trace scope check fires."""
    from forge_lens.security.keys import generate_key

    store = SQLiteTraceStore(tmp_path / "w.db")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "w.db", auth_mode="keys"
    )
    app = create_app(store=store, config=cfg)
    _, secret, h = generate_key()
    store.create_api_key("root", name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": secret}
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "w1", "role": "writer",
                             "projects": ["p1"]})
            wk = {"X-Forge-Lens-Key": r.json()["secret"]}
            foreign = synthetic_trace.model_copy(update={"project_id": "p2"})
            env = {"project_id": "p1",
                   "traces": [foreign.model_dump(mode="json")]}
            r = c.post("/v1/traces", json=env, headers=wk)
            assert r.status_code == 403
            assert store.get_trace(synthetic_trace.trace_id,
                                   project_id="p2") is None
            # control: same trace claiming p1 (envelope names p1) → 200
            ok = synthetic_trace.model_copy(update={"project_id": "p1"})
            r = c.post("/v1/traces",
                       json={"project_id": "p1",
                             "traces": [ok.model_dump(mode="json")]},
                       headers=wk)
            assert r.status_code == 200
    finally:
        store.close()


def test_gate_malformed_params_422(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    env = {"traces": [synthetic_trace.model_dump(mode="json")]}
    assert client.post("/v1/traces", json=env).status_code == 200
    tid = synthetic_trace.trace_id
    for bad in ({"x": 1}, "abc", [1]):
        r = client.post("/v1/eval/compare", json={
            "a": {"trace_ids": [tid]}, "b": {"trace_ids": [tid]},
            "gate": {"metrics": {
                "exact_match/score": {"min_pass_rate_b": bad}}}})
        assert r.status_code == 422, (bad, r.status_code, r.text)
    # NaN reaches the server — Python json.loads accepts it literally;
    # the gate must reject it rather than silently disable comparisons
    r = client.post("/v1/eval/compare", content=(
        '{"a": {"trace_ids": ["' + tid + '"]},'
        ' "b": {"trace_ids": ["' + tid + '"]},'
        ' "gate": {"metrics": {"exact_match/score": '
        '{"min_pass_rate_b": NaN}}}}'),
        headers={"content-type": "application/json"})
    assert r.status_code == 422
    r = client.post("/v1/eval/compare", json={
        "a": {"trace_ids": [tid]}, "b": {"trace_ids": [tid]},
        "gate": {"metrics": {
            "exact_match/score": {"max_drop": {"x": 1}}}}})
    assert r.status_code == 422
    r = client.post("/v1/eval/compare", json={
        "a": {"trace_ids": [tid]}, "b": {"trace_ids": [tid]},
        "gate": {"fail_on_significant_regression": {"x": 1}}})
    assert r.status_code == 422


def test_dataset_version_immutable_and_pid_typed(
    client: TestClient,
) -> None:
    r = client.put("/v1/datasets/ds1", json={"name": "d"})
    assert r.status_code == 200
    case = {"case_id": "c1", "input": {"x": 1}, "expect": {"y": 2}}
    r = client.put("/v1/datasets/ds1/versions/v1",
                   json={"cases": [case]})
    assert r.status_code == 200
    # second PUT on the same version → 409, version is immutable
    r = client.put("/v1/datasets/ds1/versions/v1",
                   json={"cases": [{**case, "input": {"x": 9}}]})
    assert r.status_code == 409
    v = client.get("/v1/datasets/ds1/versions/v1").json()
    assert v["cases"][0]["input"] == {"x": 1}  # original truth intact
    # non-string project_id → 422, not silent "5"
    r = client.put("/v1/datasets/ds2",
                   json={"name": "d", "project_id": 5})
    assert r.status_code == 422
    r = client.put("/v1/datasets/ds1/versions/v2",
                   json={"cases": [case], "project_id": {"x": 1}})
    assert r.status_code == 422


def test_eval_omitted_pid_resolves_default_for_scoped_key(
    tmp_path, synthetic_trace: Trace,
) -> None:
    """Unified omit semantics: a scoped key covering the default
    project may omit project_id on eval routes — same convention as
    GET routes (resolved once, used for check and query)."""
    from forge_lens.security.keys import generate_key

    store = SQLiteTraceStore(tmp_path / "e.db")
    store.upsert_trace(synthetic_trace.model_copy(
        update={"project_id": "default"}))
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "e.db", auth_mode="keys"
    )
    app = create_app(store=store, config=cfg)
    _, secret, h = generate_key()
    store.create_api_key("root", name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": secret}
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "d", "role": "reader",
                             "projects": ["default"]})
            dk = {"X-Forge-Lens-Key": r.json()["secret"]}
            r = c.post("/v1/eval/run", headers=dk, json={
                "trace_ids": [synthetic_trace.trace_id],
                "evaluators": [{"evaluator": "exact_match",
                                "metric": "score"}]})
            assert r.status_code == 200
            r = c.post("/v1/intel/regress", headers=dk, json={
                "a": {"trace_ids": [synthetic_trace.trace_id]},
                "b": {"trace_ids": [synthetic_trace.trace_id]}})
            assert r.status_code == 200
    finally:
        store.close()


def test_scoped_admin_cannot_escape_scope(tmp_path) -> None:
    """P0: a p1-scoped admin key must not mint unscoped/foreign keys,
    revoke keys outside its scope, list foreign keys, or read the
    global audit trail."""
    from forge_lens.security.keys import generate_key

    store = SQLiteTraceStore(tmp_path / "a.db")
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "a.db", auth_mode="keys"
    )
    app = create_app(store=store, config=cfg)
    _, root_secret, h = generate_key()
    store.create_api_key("root", name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": root_secret}
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "sa", "role": "admin",
                             "projects": ["p1"]})
            sa = {"X-Forge-Lens-Key": r.json()["secret"]}
            sa_id = r.json()["key_id"]

            # minting beyond scope → 403 (foreign and unscoped)
            for proj in (["p2"], None, []):
                body = {"name": "esc", "role": "admin"}
                if proj is not None:
                    body["projects"] = proj
                assert c.post("/v1/admin/keys", json=body,
                              headers=sa).status_code == 403, proj
            # minting inside scope → 200
            r = c.post("/v1/admin/keys", headers=sa,
                       json={"name": "ok", "role": "reader",
                             "projects": ["p1"]})
            assert r.status_code == 200
            victim_id = r.json()["key_id"]
            # revoking the unscoped root key → 403
            assert c.delete("/v1/admin/keys/root",
                            headers=sa).status_code == 403
            # revoking an in-scope key → 200
            assert c.delete(f"/v1/admin/keys/{victim_id}",
                            headers=sa).status_code == 200
            # key list shows only in-scope keys (root is hidden)
            mine = c.get("/v1/admin/keys", headers=sa).json()
            assert all(k["projects"] == ["p1"] for k in mine)
            assert "root" not in {k["key_id"] for k in mine}
            # audit trail → 403 for scoped admins
            assert c.get("/v1/admin/audit", headers=sa).status_code == 403
            assert c.get("/v1/admin/audit", headers=admin).status_code == 200
            assert sa_id
    finally:
        store.close()


def test_lone_surrogate_strings_422_not_500(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    # \ud800 escape in raw JSON — json.loads decodes it to a lone
    # surrogate; the strict client serializer can't emit one directly
    esc = "x\\ud800"
    def _post(url: str, raw: str) -> int:
        return client.post(url, content=raw,
                           headers={"content-type": "application/json"}
                           ).status_code

    def _put(url: str, raw: str) -> int:
        return client.put(url, content=raw,
                          headers={"content-type": "application/json"}
                          ).status_code

    # OTLP — json.loads path (native path is already safe via jiter)
    assert _post("/v1/otlp/traces", (
        '{"resourceSpans": [{"scopeSpans": [{"spans": [{'
        '"traceId": "' + "ab" * 16 + '", "spanId": "' + "cd" * 8 + '",'
        '"name": "' + esc + '", "startTimeUnixNano": "1",'
        '"endTimeUnixNano": "2"}]}]}]}')) == 422
    # datasets
    assert _put("/v1/datasets/ds-s",
                '{"name": "' + esc + '"}') == 422
    assert _put("/v1/datasets/ds-ok", '{"name": "d"}') == 200
    assert _put("/v1/datasets/ds-ok/versions/v1",
                '{"cases": [{"case_id": "' + esc + '"}]}') == 422
    assert _put("/v1/datasets/ds-ok/versions/v1", (
        '{"cases": [{"case_id": "c1"}],'
        ' "description": "' + esc + '"}')) == 422
    # prune + admin keys
    assert _post("/v1/admin/prune", (
        '{"before": "2030-01-01T00:00:00Z",'
        ' "project_id": "' + esc + '"}')) == 422


def test_nonfinite_boundary_numbers_422(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    env = {"traces": [synthetic_trace.model_dump(mode="json")]}
    assert client.post("/v1/traces", json=env).status_code == 200
    tid = synthetic_trace.trace_id
    # NaN/Infinity literals reach json.loads — raw content needed
    for bad in ("NaN", "Infinity"):
        r = client.post("/v1/admin/prune", content=(
            '{"older_than_days": ' + bad + '}'),
            headers={"content-type": "application/json"})
        assert r.status_code == 422, (bad, r.status_code)
        r = client.post(
            f"/v1/analyze/loop_detection_specialist/{tid}", content=(
                '{"budget": {"max_iterations": ' + bad + '}}'),
            headers={"content-type": "application/json"})
        assert r.status_code == 422, (bad, r.status_code)


def test_int64_overflow_timestamps_422(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    """Datetimes beyond the int64 ns range (~1678-2262) must 422, never
    reach the SQLite INTEGER bind as OverflowError."""
    for before in ("9999-12-31T00:00:00Z", "1000-01-01T00:00:00Z"):
        r = client.post("/v1/admin/prune", json={"before": before})
        assert r.status_code == 422, (before, r.status_code, r.text)
    assert client.post("/v1/admin/prune",
                       json={"older_than_days": 1e9}).status_code == 422
    # dataset created_at
    assert client.put("/v1/datasets/ds-y2500", json={
        "name": "d", "created_at": "2500-01-01T00:00:00Z"
    }).status_code == 422
    # native ingest — a span timestamped in year 9999
    tr = synthetic_trace.model_dump(mode="json")
    tr["started_at"] = "9999-12-31T00:00:00Z"
    tr["spans"][0]["start_time"] = "9999-12-31T00:00:00Z"
    tr["spans"][0]["end_time"] = "9999-12-31T00:00:01Z"
    r = client.post("/v1/traces", json={"traces": [tr]})
    assert r.status_code == 422, (r.status_code, r.text[:200])


def test_require_no_error_non_bool_defaults_true(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    env = {"traces": [synthetic_trace.model_dump(mode="json")]}
    assert client.post("/v1/traces", json=env).status_code == 200
    r = client.post("/v1/eval/run", json={
        "trace_ids": [synthetic_trace.trace_id],
        "evaluators": [{"evaluator": "task_completion",
                        "metric": "completed",
                        "params": {"require_no_error": "false"}}]})
    assert r.status_code == 200  # truthy string treated as default True


def test_cost_analyst_works_via_api_and_context_cannot_500(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    """The pricing registry is injected server-side — a client-supplied
    'pricing' context value is ignored, never an AttributeError → 500."""
    env = {"traces": [synthetic_trace.model_dump(mode="json")]}
    assert client.post("/v1/traces", json=env).status_code == 200
    tid = synthetic_trace.trace_id
    for bad_pricing in ({}, "x", [1], 42):
        r = client.post(f"/v1/analyze/cost_analyst/{tid}",
                        json={"context": {"pricing": bad_pricing}})
        assert r.status_code == 200, (bad_pricing, r.status_code, r.text)
    # hostile non-positive/non-finite budgets → 422
    for bad_budget in (0, -1, "nan"):
        r = client.post(f"/v1/analyze/cost_analyst/{tid}", json={
            "budget": {"max_iterations": bad_budget}})
        assert r.status_code == 422, (bad_budget, r.status_code)


def test_empty_project_id_rejected(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    tr = synthetic_trace.model_dump(mode="json")
    tr["project_id"] = ""
    r = client.post("/v1/traces", json={"traces": [tr]})
    assert r.status_code == 422, (r.status_code, r.text)
    assert client.get(
        f"/v1/traces/{synthetic_trace.trace_id}").status_code == 404


def test_regress_surfaces_missing_trace_ids(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    env = {"traces": [synthetic_trace.model_dump(mode="json")]}
    assert client.post("/v1/traces", json=env).status_code == 200
    r = client.post("/v1/intel/regress", json={
        "a": {"trace_ids": [synthetic_trace.trace_id, "ff" * 16]},
        "b": {"trace_ids": [synthetic_trace.trace_id]}})
    assert r.status_code == 200
    assert r.json()["missing_trace_ids"]["a"] == ["ff" * 16]
