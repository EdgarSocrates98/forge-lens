"""Consolidated offensive battery — Cycle 13.1 hardening & homologation.

Drives the real surfaces — FastAPI ``TestClient``, typer ``CliRunner``
and the in-process MCP stdio loop — with hostile inputs, then
cross-checks every read/export surface for secret leakage and every
role/scope boundary for tenant escape. Each assertion encodes behavior
verified against this build; accepted residuals (verbatim bodies under
``capture=full``, free-text credentials with no credential *shape*,
validation-error verbosity) are documented in
``docs/security/threat-model.md``, not asserted away.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from forge_lens.api.app import create_app
from forge_lens.cli.main import app as cli_app
from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
    TraceIngest,
    TraceSource,
)
from forge_lens.core.config import CaptureLevel, LensConfig
from forge_lens.mcp_server import serve as mcp_serve
from forge_lens.sanitize.redact import SanitizeReport, sanitize_attributes
from forge_lens.security.keys import generate_key, hash_secret
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# Canary secrets — every one must reach the store only in redacted
# form; the raw bytes are asserted absent from every surface below.
ANTHROPIC_KEY = "sk-ant-oat01-" + "ab12" * 12
X_API_KEY_VALUE = "ka9f8d7g6h5f4d3s2a1b0c"
USERINFO_PW = "sup3r-userinfo-pw"
BASIC_BLOB = "dXNlcjpwYXNzZXdvcmQ="
KV_PASSWORD = "hunter2-db-pass"
EMAIL = "ops-oncall@corp.example"

SECRETS = [
    ANTHROPIC_KEY,
    X_API_KEY_VALUE,
    USERINFO_PW,
    BASIC_BLOB,
    KV_PASSWORD,
    EMAIL,
]


# -- helpers -------------------------------------------------------------


def _mk_app(
    tmp_path: Path, name: str = "audit.db", **cfg_over: Any
) -> tuple[Any, SQLiteTraceStore]:
    store = SQLiteTraceStore(tmp_path / name)
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / name, **cfg_over
    )
    return create_app(store=store, config=cfg), store


def _secret_trace() -> Trace:
    """One valid trace carrying every credential shape this audit covers.

    Secrets sit in *non-content* attribute keys so they exercise the
    value-scrub path (not the content-attr drop) under both capture
    levels, plus a span ``status.message`` — the free-text carrier the
    sanitizer is explicitly responsible for.
    """
    tid = "c" * 32
    llm = Span(
        trace_id=tid,
        span_id="0" * 15 + "1",
        parent_span_id=None,
        name="llm call",
        kind=SpanKind.LLM,
        start_time=_T0,
        end_time=_T0 + timedelta(milliseconds=100),
        status=Status(
            code=StatusCode.ERROR, message=f"401 unauthorized for {ANTHROPIC_KEY}"
        ),
        attributes={
            "http.url": f"https://user:{USERINFO_PW}@api.corp.io/v1/x",
            "db.url": f"postgres://admin:{USERINFO_PW}@db.internal:5432/app",
            "request.headers.dump": (
                f"Authorization: Basic {BASIC_BLOB}; "
                f"X-Api-Key: {X_API_KEY_VALUE}"
            ),
            "connect.note": f"retry with password={KV_PASSWORD} then quit",
            "vendor.key": ANTHROPIC_KEY,
            "oncall.contact": EMAIL,
            "safe.metric": {"input_tokens": 512, "llm.token_count.prompt": 9},
        },
    )
    return Trace(
        trace_id=tid,
        root_span_id=llm.span_id,
        started_at=_T0,
        ended_at=_T0 + timedelta(milliseconds=100),
        session_id="sess-audit-01",
        status=Status(code=StatusCode.ERROR),
        source=TraceSource(system="audit", sdk_name="audit-sdk"),
        spans=[llm],
    )


def _assert_no_secrets(text: str, surface: str) -> None:
    for raw in SECRETS:
        assert raw not in text, f"{surface} leaked {raw!r}"


def _native_envelope(trace: Trace) -> dict[str, Any]:
    return TraceIngest(traces=[trace]).model_dump(mode="json")


def _keys_app(
    tmp_path: Path, name: str = "keys.db"
) -> tuple[Any, SQLiteTraceStore, dict[str, str]]:
    """auth_mode='keys' app + a stored unscoped admin key."""
    app, store = _mk_app(tmp_path, name=name, auth_mode="keys")
    kid, secret, hashed = generate_key()
    store.create_api_key(kid, name="root", key_hash=hashed, role="admin")
    return app, store, {"X-Forge-Lens-Key": secret}


def _mint(
    client: TestClient, admin: dict[str, str], **kwargs: Any
) -> dict[str, str]:
    r = client.post("/v1/admin/keys", headers=admin, json=kwargs)
    assert r.status_code == 200, r.text
    return {"X-Forge-Lens-Key": r.json()["secret"]}


# -- 1. ingestion hardening -----------------------------------------------


class TestIngestionBattery:
    """Malformed/oversized/hostile bodies on both ingest endpoints."""

    def test_declared_and_actual_size_rejected_413(
        self, tmp_path: Path, synthetic_trace: Trace
    ) -> None:
        app, store = _mk_app(tmp_path, max_payload_bytes=1024)
        try:
            with TestClient(app) as c:
                body = json.dumps(_native_envelope(synthetic_trace)).encode()
                assert len(body) > 1024
                # declared-size path and actual-body path both → 413
                for url in ("/v1/traces", "/v1/otlp/traces"):
                    r = c.post(
                        url,
                        content=body,
                        headers={"Content-Type": "application/json"},
                    )
                    assert r.status_code == 413, url
        finally:
            store.close()

    def test_malformed_json_400_both_endpoints(self, tmp_path: Path) -> None:
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                for url in ("/v1/traces", "/v1/otlp/traces"):
                    for bad in (b"{broken", b'{"traces": [', b"\xff\xfe{}"):
                        r = c.post(
                            url,
                            content=bad,
                            headers={"Content-Type": "application/json"},
                        )
                        assert r.status_code == 400, (url, bad, r.text)
        finally:
            store.close()

    def test_json_depth_boundary(self, tmp_path: Path) -> None:
        """Depth cap 512 — one level over → 400 on both endpoints.

        Under the cap each parser applies its own ceiling: jiter
        (native) rejects nesting beyond ~255 itself (``json_invalid``
        → 400), while ``json.loads`` (OTLP) parses until the lexical
        512-guard. The invariant that matters at every depth: a
        nested-open bomb is a 4xx, never a stack-overflow 500.
        """
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                for url in ("/v1/traces", "/v1/otlp/traces"):
                    # 513 levels: the lexical guard fires before parsing
                    over = json.dumps(
                        {"a": json.loads("[" * 512 + "]" * 512)}
                    ).encode()
                    r = c.post(url, content=over)
                    assert r.status_code == 400, url
                    assert "nesting" in r.text
                    # at/under cap: parsed — the verdict is endpoint-
                    # specific (400 jiter / 200-422 contract) but never
                    # a server fault
                    for depth in (128, 256, 400, 511):
                        at = json.dumps(
                            {"a": json.loads("[" * depth + "]" * depth)}
                        ).encode()
                        r = c.post(url, content=at)
                        assert r.status_code < 500, (url, depth)
        finally:
            store.close()

    def test_json_depth_ignores_braces_inside_strings(
        self, tmp_path: Path
    ) -> None:
        """The lexical counter must not trip on ``{``/``[`` literals
        inside string values — a false positive would be a silent
        availability regression, a false negative a crash."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                body = json.dumps(
                    {"traces": "[" * 6000 + "]" * 6000}
                ).encode()
                # 'traces' as a string is a contract violation (422)
                # on the native route; OTLP ignores the unknown key
                # and normalizes to an empty batch (200). Either way
                # it is NOT a 400 depth verdict — proof the braces
                # were lexed as string content, not structure.
                assert c.post("/v1/traces", content=body).status_code == 422
                assert (
                    c.post("/v1/otlp/traces", content=body).status_code
                    in (200, 422)
                )
        finally:
            store.close()

    def test_wide_array_and_object_bombs(self, tmp_path: Path) -> None:
        """Flat-but-huge structures stay under the byte cap — the
        contract layer must still answer 422, not hang or 500."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                wide = json.dumps({"traces": ["x"] * 100_000}).encode()
                assert len(wide) < 8 * 1024 * 1024  # under the byte cap
                r = c.post("/v1/traces", content=wide)
                assert r.status_code == 422
                obj = json.dumps(
                    {"traces": None, **{f"k{i}": i for i in range(50_000)}}
                ).encode()
                r = c.post("/v1/traces", content=obj)
                assert r.status_code == 422
        finally:
            store.close()

    def test_huge_single_string_truncated_not_fatal(
        self, tmp_path: Path, synthetic_trace: Trace
    ) -> None:
        """A ~2 MB attribute below the payload cap is accepted but
        truncated to ``max_attr_value_chars`` with an explicit marker."""
        app, store = _mk_app(tmp_path)
        try:
            t = synthetic_trace.model_copy(deep=True)
            t.spans[0].attributes = {"blob": "A" * 2_000_000}
            with TestClient(app) as c:
                r = c.post("/v1/traces", json=_native_envelope(t))
                assert r.status_code == 200, r.text
                assert r.json()["sanitize"]["truncated_values"] >= 1
                got = c.get(f"/v1/traces/{t.trace_id}").json()
                val = got["spans"][0]["attributes"]["blob"]
                assert len(val) < 4_200
                assert val.endswith("[TRUNCATED]")
        finally:
            store.close()

    def test_int_and_float_overflow_no_500(
        self, tmp_path: Path, otlp_payload: dict[str, Any]
    ) -> None:
        """int64 overflows and non-finite doubles degrade to quarantine
        or a coerced null — never a driver-level 500."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                # timestamp beyond int64 ns → quarantined span, not 500
                p = otlp_payload
                spans = p["resourceSpans"][0]["scopeSpans"][0]["spans"]
                good_start = spans[0]["startTimeUnixNano"]
                spans[0]["startTimeUnixNano"] = "99999999999999999999999999"
                r = c.post("/v1/otlp/traces", json=p)
                assert r.status_code == 200, r.text
                assert r.json()["warnings"] or r.json()["accepted_spans"] < 3

                # giant intValue (>int64) — stored JSON-side, retrievable
                p2 = json.loads(json.dumps(otlp_payload))
                s = p2["resourceSpans"][0]["scopeSpans"][0]["spans"]
                s[0]["startTimeUnixNano"] = good_start
                s[0]["attributes"] = [
                    {"key": "big.int", "value": {"intValue": "9" * 30}}
                ]
                r = c.post("/v1/otlp/traces", json=p2)
                assert r.status_code == 200, r.text
                got = c.get(f"/v1/traces/{'b' * 32}").json()
                root = next(
                    s_ for s_ in got["spans"] if s_["name"] == "root-op"
                )
                assert "big.int" in root["attributes"]

                # doubleValue 1e400 → inf on the wire is legal JSON
                # (json.loads parses it); the strict client serializer
                # can't emit it, so the literal goes via content=.
                # Stored side: _json_finite coerces to null, readback ok.
                p3 = json.loads(json.dumps(otlp_payload))
                s3 = p3["resourceSpans"][0]["scopeSpans"][0]["spans"]
                s3[0]["startTimeUnixNano"] = good_start
                s3[0]["spanId"] = "f" * 16  # fresh span — no dedupe merge
                s3[0]["attributes"].insert(
                    0,
                    {"key": "big.float",
                     "value": {"doubleValue": "@@BIG@@"}},
                )
                raw3 = json.dumps(p3).replace('"@@BIG@@"', "1e400")
                r = c.post("/v1/otlp/traces", content=raw3.encode())
                assert r.status_code == 200, r.text
                got3 = c.get(f"/v1/traces/{'b' * 32}").json()
                float_span = next(
                    s_ for s_ in got3["spans"] if s_["span_id"] == "f" * 16
                )
                assert float_span["attributes"]["big.float"] is None
        finally:
            store.close()

    def test_native_timestamp_beyond_int64_is_422(
        self, tmp_path: Path, synthetic_trace: Trace
    ) -> None:
        app, store = _mk_app(tmp_path)
        try:
            t = synthetic_trace.model_copy(deep=True)
            t.spans[0].start_time = datetime(9999, 1, 1, tzinfo=UTC)
            with TestClient(app) as c:
                r = c.post("/v1/traces", json=_native_envelope(t))
                assert r.status_code == 422, r.text
                assert c.get(f"/v1/traces/{t.trace_id}").status_code == 404
        finally:
            store.close()

    def test_lone_surrogate_rejected_both_paths(
        self, tmp_path: Path
    ) -> None:
        """``\\ud800`` escapes are valid JSON (json.loads materializes
        the surrogate) but unbindable in SQLite — the strict jiter
        parser catches them at 400 on the native route while the
        lenient json.loads paths (OTLP, _eval_body) answer 422."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                # native — jiter refuses the unpaired escape → 400
                raw = (
                    b'{"traces": [{"trace_id": "' + b"c" * 32
                    + b'", "spans": [], "status": {"code": "ok",'
                    b' "message": "bad \\ud800 x"},'
                    b' "source": {"system": "t"},'
                    b' "started_at": "2026-01-01T00:00:00Z"}]}'
                )
                r = c.post("/v1/traces", content=raw)
                assert r.status_code == 400, r.text
                # OTLP — json.loads keeps the surrogate → 422
                otlp = (
                    '{"resourceSpans": [{"scopeSpans": [{"spans": [{'
                    '"traceId": "' + "ab" * 16 + '", "spanId": "'
                    + "cd" * 8 + '", "name": "x\\ud800",'
                    ' "startTimeUnixNano": "1",'
                    ' "endTimeUnixNano": "2"}]}]}]}'
                )
                r = c.post("/v1/otlp/traces", content=otlp.encode())
                assert r.status_code == 422, r.text
                # _eval_body route — same lenient-parse guard → 422
                r = c.post(
                    "/v1/intel/efficiency",
                    content=(
                        b'{"trace_ids": ["\\ud800"],'
                        b' "project_id": "p1"}'
                    ),
                )
                assert r.status_code == 422, r.text
        finally:
            store.close()

    def test_content_type_confusion(self, tmp_path: Path, otlp_payload: dict[str, Any]) -> None:
        """OTLP route hard-rejects binary encodings (415); both routes
        parse JSON regardless of the declared type — an intentional
        posture documented in the threat model."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                body = json.dumps(otlp_payload).encode()
                for ct in (
                    "application/x-protobuf",
                    "application/octet-stream",
                ):
                    r = c.post(
                        "/v1/otlp/traces", content=body,
                        headers={"Content-Type": ct},
                    )
                    assert r.status_code == 415, ct
                # missing / wrong content-type with valid JSON still parses
                r = c.post("/v1/otlp/traces", content=body)
                assert r.status_code == 200
                r = c.post(
                    "/v1/otlp/traces", content=body,
                    headers={"Content-Type": "text/plain"},
                )
                assert r.status_code == 200
                r = c.post("/v1/traces", content=b"{}")
                assert r.status_code == 422  # parsed, contract-invalid
        finally:
            store.close()

    def test_rate_limit_429(self, tmp_path: Path, synthetic_trace: Trace) -> None:
        """Fixed-window limiter: requests past ``rate_limit_per_minute``
        get 429 via RateLimitedError → _ingest_error."""
        app, store = _mk_app(tmp_path, rate_limit_per_minute=2)
        try:
            with TestClient(app) as c:
                env = _native_envelope(synthetic_trace)
                codes = [
                    c.post("/v1/traces", json=env).status_code
                    for _ in range(4)
                ]
                assert codes[:2] == [200, 200]
                assert codes[2:] == [429, 429]
        finally:
            store.close()

    def test_query_param_bounds(self, tmp_path: Path) -> None:
        """int64 query bounds — arbitrary-precision ints and non-finite
        floats must be 422, never a sqlite bind error (500)."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                assert c.get(
                    "/v1/traces", params={"offset": 2**63}
                ).status_code == 422
                assert c.get(
                    "/v1/traces", params={"min_total_tokens": 2**63}
                ).status_code == 422
                assert c.get(
                    "/v1/traces", params={"min_duration_ms": 1e400}
                ).status_code == 422
                assert c.get(
                    "/v1/traces", params={"status": "bogus"}
                ).status_code == 422
        finally:
            store.close()

    def test_uri_encoded_path_traversal_safe(self, tmp_path: Path) -> None:
        """Percent-encoded traversal in path params is bound as data —
        a 404 lookup, never a filesystem reach."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                for probe in (
                    "%2e%2e%2f..",
                    "..%2flens.db",
                    "%00",
                    "..%2f..%2fetc%2fpasswd",
                ):
                    r = c.get(f"/v1/traces/{probe}")
                    assert r.status_code in (400, 404), (probe, r.status_code)
        finally:
            store.close()


# -- 2. sanitization across every read surface ----------------------------


class TestSanitizationBattery:
    """Secrets crafted into a trace must appear on NO read/export
    surface in raw form — and structural ids must survive."""

    def test_secret_trace_on_all_surfaces(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        app, store = _mk_app(tmp_path)
        t = _secret_trace()
        try:
            with TestClient(app) as c:
                r = c.post("/v1/traces", json=_native_envelope(t))
                assert r.status_code == 200, r.text
                # sanitize counters are disclosed on the ingest response
                s = r.json()["sanitize"]
                assert s["redacted_values"] >= 6, s
                assert s["emails_masked"] >= 1, s

                # API reads
                tid = t.trace_id
                read = c.get(f"/v1/traces/{tid}")
                assert read.status_code == 200
                body = json.dumps(read.json())
                _assert_no_secrets(body, "GET /v1/traces/{id}")
                assert tid in body
                assert "0" * 15 + "1" in body
                assert "sess-audit-01" in body

                # incident export — derived bundle re-scrubbed
                inc = c.get(f"/v1/traces/{tid}/incident")
                assert inc.status_code == 200
                _assert_no_secrets(json.dumps(inc.json()), "incident")
                assert set(inc.json()["export_sanitize"]) == {
                    "redacted_keys",
                    "redacted_values",
                    "emails_masked",
                    "dropped_content",
                }

                # SSE stream, list, ops summary, failure
                for url in (
                    f"/v1/traces/{tid}/stream",
                    "/v1/traces",
                    "/v1/ops/summary",
                    f"/v1/traces/{tid}/failure",
                    f"/v1/traces/{tid}/replay",
                    f"/v1/traces/{tid}/security",
                    f"/v1/graph/traces/{tid}",
                ):
                    r = c.get(url)
                    assert r.status_code == 200, (url, r.status_code)
                    _assert_no_secrets(r.text, url)
        finally:
            store.close()

        # CLI read surface — same store, operator view
        db = tmp_path / "audit.db"
        runner = CliRunner()
        for extra in ([], ["--json"]):
            r = runner.invoke(
                cli_app,
                ["traces", "show", t.trace_id, "--db", str(db),
                 "--project", "default", *extra],
            )
            assert r.exit_code == 0, r.output
            _assert_no_secrets(r.output, f"cli show {extra}")
            assert t.trace_id in r.output

        # MCP stdio observer — readonly reopen of the same db
        out = _mcp_tools_call(
            db,
            [
                ("lens.get_trace", {"trace_id": t.trace_id}),
                ("lens.failure", {"trace_id": t.trace_id}),
                ("lens.cost", {"trace_id": t.trace_id}),
                ("lens.list_traces", {}),
                ("lens.stats", {}),
            ],
            capsys,
        )
        blob = json.dumps(out)
        _assert_no_secrets(blob, "mcp tools")
        assert t.trace_id in blob

    def test_capture_full_still_value_scrubs(
        self, tmp_path: Path
    ) -> None:
        """Opt-in ``capture=full`` keeps content attrs but the value
        scrub still applies INSIDE them — a credential shape in a
        captured body must not survive verbatim."""
        app, store = _mk_app(tmp_path, capture=CaptureLevel.FULL)
        t = _secret_trace()
        t.spans[0].attributes["input.value"] = (
            f"call https://user:{USERINFO_PW}@h with token {ANTHROPIC_KEY}"
        )
        try:
            with TestClient(app) as c:
                r = c.post("/v1/traces", json=_native_envelope(t))
                assert r.status_code == 200, r.text
                body = json.dumps(c.get(f"/v1/traces/{t.trace_id}").json())
                for raw in (ANTHROPIC_KEY, USERINFO_PW, KV_PASSWORD,
                            BASIC_BLOB, X_API_KEY_VALUE):
                    assert raw not in body
                # accepted residual at full: e-mail + free-text stays
                assert EMAIL in body
        finally:
            store.close()

    def test_free_text_password_without_shape_is_residual(
        self,
    ) -> None:
        """Homologation honesty: ``the password is hunter2`` (no
        ``=``/``:`` shape) is NOT redacted — documented accepted gap."""
        rep = SanitizeReport()
        out = sanitize_attributes(
            {"note": "the password is hunter2 — written in prose"},
            rep,
            capture=CaptureLevel.METADATA,
            max_chars=4096,
        )
        assert "hunter2" in out["note"]  # residual, see threat-model

    def test_new_value_forms_redacted(self) -> None:
        """Regression for the userinfo/kv/authorization-scheme gap:
        credential *shapes* inside free text die, structure survives."""
        rep = SanitizeReport()
        out = sanitize_attributes(
            {
                "a": "https://user:pw@h.example:8443/x",
                "b": "postgres://u:p@db:5432/app",
                "c": "password=hunter2",
                "d": "X-Api-Key: abcdef0123456789",
                "e": "Authorization: Basic dXNlcg==",
                "f": "accessToken=tok_abc123",
                "g": 'quoted "password": "s3cret"',
            },
            rep,
            capture=CaptureLevel.METADATA,
            max_chars=4096,
        )
        for leak in ("pw@", "u:p@", "hunter2", "abcdef0123456789",
                     "dXNlcg==", "tok_abc123", "s3cret"):
            assert leak not in json.dumps(out), leak
        # correlation structure kept
        assert "h.example:8443" in out["a"]
        assert out["c"].startswith("password=")
        # safe lookalikes untouched
        rep2 = SanitizeReport()
        safe = sanitize_attributes(
            {
                "m1": "input_tokens=512 llm.token_count.prompt=9",
                "m2": "sort_key=name partition_key=abc",
                "m3": "https://api.example.com:8080/path",
                "m4": "user@host.example",
            },
            rep2,
            capture=CaptureLevel.METADATA,
            max_chars=4096,
        )
        assert safe["m1"] == "input_tokens=512 llm.token_count.prompt=9"
        assert safe["m2"] == "sort_key=name partition_key=abc"
        assert safe["m3"] == "https://api.example.com:8080/path"


# -- 3. authorization battery ---------------------------------------------


class TestAuthorizationBattery:
    """``auth_mode='keys'``: role order, tenant scope, key validity,
    admin-only routes and the audit trail. Plus the documented open
    posture of ``none`` and fail-fast on a typo'd mode."""

    def test_roles_matrix(self, tmp_path: Path, synthetic_trace: Trace) -> None:
        app, store, admin = _keys_app(tmp_path)
        try:
            with TestClient(app) as c:
                reader = _mint(c, admin, name="ro", role="reader",
                               projects=["p1"])
                writer = _mint(c, admin, name="rw", role="writer",
                               projects=["p1"])
                # envelope project_id fills default traces → in-scope
                env = json.dumps(
                    TraceIngest(
                        traces=[synthetic_trace], project_id="p1"
                    ).model_dump(mode="json")
                )

                # reader: reads ok in scope, writes 403
                assert c.get("/v1/traces?project_id=p1",
                             headers=reader).status_code == 200
                assert c.post("/v1/traces", content=env,
                              headers=reader).status_code == 403
                assert c.put(
                    "/v1/datasets/ds/versions/v1",
                    json={"project_id": "p1", "cases": []},
                    headers=reader,
                ).status_code == 403
                assert c.post("/v1/experiments/x/run", json={
                    "project_id": "p1", "gate": {}},
                    headers=reader).status_code == 403

                # writer: writes ok in scope, admin surface 403
                r = c.post("/v1/traces", content=env, headers=writer)
                assert r.status_code == 200, r.text
                for url in (
                    "/v1/admin/keys", "/v1/admin/audit",
                    "/v1/admin/prune", "/v1/alerts/rules",
                ):
                    for method in (c.get, c.post):
                        r = method(url, headers=writer)
                        # 403 = role check fired; 405 = method doesn't
                        # exist at all — both are safe non-access
                        assert r.status_code in (403, 405), (
                            url, r.status_code)
                assert c.delete(
                    "/v1/admin/keys/flk_x", headers=writer
                ).status_code == 403
                assert c.delete(
                    "/v1/alerts/rules/r1", headers=writer
                ).status_code == 403
        finally:
            store.close()

    def test_tenant_isolation_reads_and_writes(
        self, tmp_path: Path, synthetic_trace: Trace
    ) -> None:
        app, store, admin = _keys_app(tmp_path)
        victim = synthetic_trace.model_copy(update={"project_id": "p2"})
        store.upsert_trace(victim)
        store.upsert_dataset("ds-p2", project_id="p2", name="victim-ds")
        try:
            with TestClient(app) as c:
                scoped_r = _mint(c, admin, name="r", role="reader",
                                 projects=["p1"])
                scoped_w = _mint(c, admin, name="w", role="writer",
                                 projects=["p1"])
                tid = synthetic_trace.trace_id
                for url in (
                    "/v1/traces?project_id=p2",
                    f"/v1/traces/{tid}?project_id=p2",
                    "/v1/datasets?project_id=p2",
                    "/v1/datasets/ds-p2?project_id=p2",
                    "/v1/experiments?project_id=p2",
                    "/v1/ops/summary?project_id=p2",
                    "/v1/sessions?project_id=p2",
                    "/v1/tools?project_id=p2",
                ):
                    r = c.get(url, headers=scoped_r)
                    assert r.status_code == 403, (url, r.status_code)
                # cross-project write denied; own-project ok
                env_p2 = json.dumps(
                    TraceIngest(traces=[victim]).model_dump(mode="json")
                )
                assert c.post("/v1/traces", content=env_p2,
                              headers=scoped_w).status_code == 403
                # omitted project_id resolves to 'default' → still 403
                assert c.get("/v1/traces",
                             headers=scoped_r).status_code == 403
        finally:
            store.close()

    def test_invalid_revoked_and_crafted_keys(
        self, tmp_path: Path
    ) -> None:
        app, store, admin = _keys_app(tmp_path)
        try:
            with TestClient(app) as c:
                # garbage / crafted payloads → 401, never an identity
                for bad in (
                    "flk_sk_wrong",
                    '{"role":"admin","projects":[]}',
                    "admin",
                    "0" * 64,  # a bare sha256 hex is not a usable secret
                ):
                    assert c.get(
                        "/v1/health", headers={"X-Forge-Lens-Key": bad}
                    ).status_code == 401, bad
                # the stored key_id presented as the secret → 401
                rec = c.get("/v1/admin/keys", headers=admin).json()[0]
                assert c.get(
                    "/v1/health",
                    headers={"X-Forge-Lens-Key": rec["key_id"]},
                ).status_code == 401
                # revoked key → 401
                minted = _mint(c, admin, name="tmp", role="reader")
                kid = c.get("/v1/admin/keys", headers=admin).json()
                tmp_id = next(k["key_id"] for k in kid if k["name"] == "tmp")
                assert c.delete(f"/v1/admin/keys/{tmp_id}",
                                headers=admin).status_code == 200
                assert c.get("/v1/health", headers=minted).status_code == 401
                # key with an out-of-band bogus role fails CLOSED (403)
                bogus_secret = "flk_sk_bogus-role"
                store.create_api_key(
                    "flk_bogus", name="planted",
                    key_hash=hash_secret(bogus_secret), role="superuser",
                )
                assert c.get(
                    "/v1/health",
                    headers={"X-Forge-Lens-Key": bogus_secret},
                ).status_code == 403
        finally:
            store.close()

    def test_reader_cannot_escalate_via_mint(
        self, tmp_path: Path
    ) -> None:
        app, store, admin = _keys_app(tmp_path)
        try:
            with TestClient(app) as c:
                reader = _mint(c, admin, name="ro", role="reader")
                # mint attempts as reader — 403 before body eval
                r = c.post("/v1/admin/keys", headers=reader,
                           json={"name": "x", "role": "admin"})
                assert r.status_code == 403
                # a reader's own key must never gain a new role
                assert c.get("/v1/admin/keys",
                             headers=reader).status_code == 403
        finally:
            store.close()

    def test_admin_routes_enumerated(self, tmp_path: Path) -> None:
        """Every admin-only route rejects unauthenticated (401) and
        non-admin (403) callers — enumerated, not sampled."""
        app, store, admin = _keys_app(tmp_path)
        try:
            with TestClient(app) as c:
                reader = _mint(c, admin, name="r", role="reader")
                calls: list[tuple[str, Any, str, Any]] = [
                    ("post", c.post, "/v1/admin/keys",
                     {"name": "x", "role": "reader"}),
                    ("get", c.get, "/v1/admin/keys", None),
                    ("delete", c.delete, "/v1/admin/keys/flk_x", None),
                    ("get", c.get, "/v1/admin/audit", None),
                    ("post", c.post, "/v1/admin/prune",
                     {"older_than_days": 0}),
                    ("post", c.post, "/v1/alerts/rules",
                     {"rule_id": "r", "metric": "m", "threshold": 1}),
                    ("delete", c.delete, "/v1/alerts/rules/r", None),
                ]
                for name, fn, url, payload in calls:
                    r = (
                        fn(url, headers=reader, json=payload)
                        if payload is not None and name != "get"
                        else fn(url, headers=reader)
                    )
                    assert r.status_code == 403, (name, url, r.status_code)
                    r = (
                        fn(url, json=payload)
                        if payload is not None and name != "get"
                        else fn(url)
                    )
                    assert r.status_code == 401, (name, url, r.status_code)
        finally:
            store.close()

    def test_mutations_leave_audit_records(self, tmp_path: Path) -> None:
        app, store, admin = _keys_app(tmp_path)
        try:
            with TestClient(app) as c:
                reader = _mint(c, admin, name="ro", role="reader")
                c.get("/v1/admin/keys", headers=reader)  # denied → auth event
                c.post("/v1/admin/prune", headers=admin,
                       json={"older_than_days": 36500, "all_projects": True})
                events = c.get("/v1/admin/audit", headers=admin).json()
                actions = {e["action"] for e in events}
                assert {"key.create", "auth", "traces.prune"} <= actions
                # denied auth event exists and carries no secret material
                denied = [e for e in events if e["outcome"] == "denied"]
                assert denied
                blob = json.dumps(events)
                assert "flk_sk_" not in blob
                for e in events:
                    assert "secret" not in json.dumps(e["detail"])
        finally:
            store.close()

    def test_auth_mode_none_open_posture(self, tmp_path: Path) -> None:
        """Documented local-first posture: no auth anywhere, admin
        surface included — the mode is a trust statement, not a bug."""
        app, store = _mk_app(tmp_path)
        try:
            with TestClient(app) as c:
                assert c.get("/v1/traces").status_code == 200
                assert c.get("/v1/admin/keys").status_code == 200
                assert c.get("/v1/admin/audit").status_code == 200
        finally:
            store.close()

    def test_auth_mode_unknown_fails_at_boot(self, tmp_path: Path) -> None:
        """A typo'd auth_mode must not silently boot: unverified modes
        previously fell through to open reads + dead writes."""
        store = SQLiteTraceStore(tmp_path / "b.db")
        try:
            cfg = LensConfig(
                data_dir=tmp_path, db_path=tmp_path / "b.db",
                auth_mode="keyz",
            )
            with pytest.raises(ValueError, match="auth_mode"):
                create_app(store=store, config=cfg)
        finally:
            store.close()


# -- MCP harness (same pattern as tests/unit/test_mcp_tools.py) ------------


def _mcp_tools_call(
    db: Path,
    calls: list[tuple[str, dict[str, Any]]],
    capsys: pytest.CaptureFixture[str],
) -> dict[int, Any]:
    init = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "audit", "version": "0"},
        },
    }
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": i + 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        }
        for i, (name, args) in enumerate(calls)
    ]
    stdin = io.StringIO(
        "".join(json.dumps(m) + "\n" for m in [init, *msgs])
    )
    old = sys.stdin
    sys.stdin = stdin
    try:
        mcp_serve(str(db))
    finally:
        sys.stdin = old
    out = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line
    ]
    return {o["id"]: o for o in out}


class TestMcpSurface:
    """The stdio observer gates on initialize and returns opaque
    internal errors — never stack text or payload echoes."""

    def test_lifecycle_and_opaque_errors(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db = tmp_path / "m.db"
        store = SQLiteTraceStore(db)
        store.close()
        # call before initialize → protocol error, no tool dispatch
        early = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "lens.stats", "arguments": {}},
        }
        stdin = io.StringIO(json.dumps(early) + "\n")
        old = sys.stdin
        sys.stdin = stdin
        try:
            mcp_serve(str(db))
        finally:
            sys.stdin = old
        out = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line
        ]
        assert out[0]["id"] == 1
        assert "error" in out[0]

    def test_tool_arg_error_is_opaque(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Authored TypeError arg-validation → -32602 with a STATIC
        message — arg detail can embed payload fragments, so it never
        crosses the wire."""
        db = tmp_path / "m.db"
        store = SQLiteTraceStore(db)
        store.close()
        out = _mcp_tools_call(
            db,
            [("lens.get_trace", {"trace_id": 123})],  # TypeError inside
            capsys,
        )
        resp = out[1]
        assert resp["error"]["code"] == -32602
        assert resp["error"]["message"] == "invalid params"
        # the real TypeError text ("trace_id must be a non-empty
        # string") must not leak
        assert "non-empty" not in json.dumps(resp)
