"""Cycle 08 — keys, RBAC, tenancy, audit, retention, MCP observer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_lens.contracts.v1.trace import Span, SpanKind, Status, StatusCode
from forge_lens.security.keys import (
    KeyIdentity,
    generate_key,
    hash_secret,
    project_allowed,
    role_at_least,
)
from forge_lens.security.mcp_inspect import inspect_span, inspect_trace
from forge_lens.storage.sqlite import SQLiteTraceStore


def test_key_lifecycle(tmp_store: SQLiteTraceStore) -> None:
    key_id, secret, hashed = generate_key()
    assert secret.startswith("flk_sk_")
    assert len(hashed) == 64
    tmp_store.create_api_key(
        key_id, name="ci", key_hash=hashed, role="writer", projects=["p1"]
    )
    rec = tmp_store.api_key_by_hash(hash_secret(secret))
    assert rec is not None
    assert rec["key_id"] == key_id
    assert rec["role"] == "writer"
    assert rec["projects"] == ["p1"]
    # wrong secret → no record
    assert tmp_store.api_key_by_hash(hash_secret("flk_sk_nope")) is None
    # revoke → lookup fails
    assert tmp_store.revoke_api_key(key_id, now_ns=1)
    assert tmp_store.api_key_by_hash(hash_secret(secret)) is None
    assert not tmp_store.revoke_api_key(key_id)


def test_role_and_scope_rules() -> None:
    assert role_at_least("admin", "writer")
    assert role_at_least("writer", "reader")
    assert not role_at_least("reader", "writer")
    scoped = KeyIdentity(actor="k", role="writer", projects=("p1",))
    unscoped = KeyIdentity(actor="k", role="admin", projects=())
    assert project_allowed(scoped, "p1")
    assert not project_allowed(scoped, "p2")
    assert not project_allowed(scoped, None)
    assert project_allowed(unscoped, None)


def test_audit_and_prune(tmp_store: SQLiteTraceStore, synthetic_trace) -> None:
    tmp_store.upsert_trace(synthetic_trace)
    tmp_store.record_audit(
        actor="cli", action="traces.prune", resource="*",
        outcome="ok", detail={"n": 1}, ts_ns=123,
    )
    events = tmp_store.list_audit()
    assert len(events) == 1
    assert events[0]["action"] == "traces.prune"
    assert events[0]["detail"]["n"] == 1
    # prune before the trace start → nothing; after → the trace
    start_ns = 1_000  # far past — trace starts 2026
    assert tmp_store.prune_traces(before_ns=start_ns)["traces"] == 0
    far_future = 4_102_444_800_000_000_000  # 2100-01-01
    assert tmp_store.prune_traces(before_ns=far_future)["traces"] == 1
    assert tmp_store.get_trace(synthetic_trace.trace_id) is None


def test_mcp_inspect_flags_and_stays_an_observer(
    synthetic_trace,
) -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    span = Span(
        trace_id="a" * 32, span_id="9" * 16,
        name="tool os.system", kind=SpanKind.TOOL,
        start_time=t0, end_time=t0 + timedelta(milliseconds=1),
        status=Status(code=StatusCode.OK),
        attributes={
            "tool.name": "os.system",
            "tool.args": {"cmd": "cat ~/.ssh/id_rsa"},
        },
    )
    obs = inspect_span(span)
    kinds = {o["kind"] for o in obs}
    assert "risky_tool" in kinds
    assert "sensitive_path" in kinds
    report = inspect_trace(synthetic_trace)
    assert report["mode"] == "observer"
    assert isinstance(report["observations"], list)
