"""Cycle 13 — MCP stdio observer tools over the read-intelligence surface.

Drives ``forge_lens.mcp_server.serve`` in-process: ``sys.stdin`` is
swapped for a StringIO of newline-delimited JSON-RPC frames and stdout
is captured, exactly like ``test_mcp_server_survives_bad_call`` in
``test_ops.py``.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.intel.pricing import PRICING_SOURCE_ID
from forge_lens.mcp_server import serve
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
LOOP_TID = "b" * 32


def _loop_trace() -> Trace:
    """Four identical tool calls — detect_loops must flag the run."""
    spans = [
        Span(
            trace_id=LOOP_TID,
            span_id=f"{i + 1:016x}",
            name="tool read",
            kind=SpanKind.TOOL,
            start_time=_T0 + timedelta(milliseconds=i),
            end_time=_T0 + timedelta(milliseconds=i + 1),
            status=Status(code=StatusCode.OK),
            attributes={"tool.args": {"path": "/x"}},
        )
        for i in range(4)
    ]
    return Trace(
        trace_id=LOOP_TID,
        root_span_id=spans[0].span_id,
        started_at=_T0,
        ended_at=_T0 + timedelta(milliseconds=4),
        status=Status(code=StatusCode.OK),
        spans=spans,
    )


@pytest.fixture
def seeded_db(
    tmp_path: Path, synthetic_trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A closed store on disk — ``serve`` reopens it readonly.

    ``load_config()`` (called by ``serve`` for the pricing registry)
    reads ``cwd/.forge-lens/config.toml`` and ``FORGE_LENS_*`` env, so
    tests pin both for determinism.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_LENS_PRICING_FILE", raising=False)
    db = tmp_path / "m.db"
    store = SQLiteTraceStore(db)
    store.upsert_trace(synthetic_trace)
    store.upsert_trace(_loop_trace())
    store.upsert_dataset("ds-1", project_id="default", name="demo")
    store.close()
    return db


def _rpc(method: str, rid: int, **params: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params:
        msg["params"] = params
    return msg


def _call_tool(rid: int, name: str, arguments: Any = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return _rpc("tools/call", rid, **params)


def _serve(
    db: Path,
    msgs: list[dict[str, Any]],
    capsys: pytest.CaptureFixture[str],
) -> dict[int, dict[str, Any]]:
    init = {
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "0"}},
    }
    stdin = io.StringIO(
        "".join(json.dumps(m) + "\n" for m in [init, *msgs]))
    old = sys.stdin
    sys.stdin = stdin
    try:
        serve(str(db))
    finally:
        sys.stdin = old
    out = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line
    ]
    return {o["id"]: o for o in out}


def _payload(resp: dict[str, Any]) -> dict[str, Any]:
    """Unwrap the MCP text-content envelope into the tool's dict."""
    return json.loads(resp["result"]["content"][0]["text"])


def test_pre_init_calls_are_rejected(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """MCP requires initialize first; tool failures mark isError."""
    stdin = io.StringIO(
        json.dumps(_call_tool(9, "lens.sessions", {})) + "\n"
        + json.dumps(_rpc("tools/list", 10)) + "\n"
    )
    old = sys.stdin
    sys.stdin = stdin
    try:
        serve(str(seeded_db))
    finally:
        sys.stdin = old
    out = [json.loads(line)
           for line in capsys.readouterr().out.splitlines() if line]
    for resp in out:
        assert resp["error"]["code"] == -32602
        assert "initialize" in resp["error"]["message"]

    # After init, a data-level miss is result content with isError —
    # not a protocol fault (the loop keeps serving).
    by_id = _serve(
        seeded_db,
        [_call_tool(1, "lens.failure", {"trace_id": "f" * 32})],
        capsys,
    )
    assert by_id[1]["result"]["isError"] is True
    assert "not found" in _payload(by_id[1])["error"]


def test_tools_list_covers_read_intel(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id = _serve(seeded_db, [_rpc("tools/list", 1)], capsys)
    tools = by_id[1]["result"]["tools"]
    assert {t["name"] for t in tools} == {
        "lens.capabilities",
        "lens.correlate",
        "lens.cost",
        "lens.detect_loops",
        "lens.failure",
        "lens.get_trace",
        "lens.list_datasets",
        "lens.list_traces",
        "lens.sessions",
        "lens.tools",
        "lens.stats",
        "lens.trace_efficiency",
    }
    for t in tools:
        assert t["inputSchema"]["type"] == "object", t["name"]


def test_existing_tools_still_work(
    seeded_db: Path,
    capsys: pytest.CaptureFixture[str],
    synthetic_trace: Trace,
) -> None:
    by_id = _serve(
        seeded_db,
        [
            _call_tool(1, "lens.list_traces", {"limit": 5}),
            _call_tool(
                2, "lens.get_trace", {"trace_id": synthetic_trace.trace_id}
            ),
            _call_tool(3, "lens.stats"),
        ],
        capsys,
    )
    assert len(_payload(by_id[1])["traces"]) == 2
    assert (
        _payload(by_id[2])["trace"]["trace_id"] == synthetic_trace.trace_id
    )
    assert _payload(by_id[3])["traces"] == 2


def test_detect_loops_happy_missing_and_wrong_project(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id = _serve(
        seeded_db,
        [
            _call_tool(1, "lens.detect_loops", {"trace_id": LOOP_TID}),
            _call_tool(2, "lens.detect_loops", {"trace_id": "f" * 32}),
            _call_tool(
                3,
                "lens.detect_loops",
                {"trace_id": LOOP_TID, "project_id": "other"},
            ),
        ],
        capsys,
    )
    out = _payload(by_id[1])
    assert out["trace_id"] == LOOP_TID
    assert out["classification"] == "heuristic"
    kinds = {f["kind"] for f in out["findings"]}
    assert "repeated_identical_tool" in kinds
    # missing / wrong-project ids are data, not protocol faults
    assert _payload(by_id[2]) == {
        "trace_id": "f" * 32,
        "error": "trace not found",
    }
    assert _payload(by_id[3])["error"] == "trace not found"


def test_trace_efficiency_roll_up(
    seeded_db: Path,
    capsys: pytest.CaptureFixture[str],
    synthetic_trace: Trace,
) -> None:
    by_id = _serve(
        seeded_db,
        [
            _call_tool(
                1,
                "lens.trace_efficiency",
                {"trace_id": synthetic_trace.trace_id},
            )
        ],
        capsys,
    )
    out = _payload(by_id[1])
    assert out["trace_id"] == synthetic_trace.trace_id
    assert out["tool_calls"] == 1
    assert out["llm_calls"] == 1
    assert out["error_spans"] == 1
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 42
    assert out["succeeded"] is False


def test_cost_default_bundled_registry(
    seeded_db: Path,
    capsys: pytest.CaptureFixture[str],
    synthetic_trace: Trace,
) -> None:
    by_id = _serve(
        seeded_db,
        [_call_tool(1, "lens.cost", {"trace_id": synthetic_trace.trace_id})],
        capsys,
    )
    out = _payload(by_id[1])
    # same shape as GET /v1/traces/{id}/cost
    assert out["pricing_source"] == PRICING_SOURCE_ID
    assert out["provenance"] == "derived"
    assert "gpt-test" in out["unpriced_models"]
    assert out["total_input_tokens"] == 100
    assert out["total_output_tokens"] == 42
    assert isinstance(out["waste"], list)
    assert isinstance(out["span_costs"], list)


def test_cost_uses_pricing_file_env(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    synthetic_trace: Trace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same path as the API: FORGE_LENS_PRICING_FILE → from_file."""
    monkeypatch.chdir(tmp_path)
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {"gpt-test": {"input_per_mtok": 1.0, "output_per_mtok": 2.0}}
        )
    )
    monkeypatch.setenv("FORGE_LENS_PRICING_FILE", str(prices))
    db = tmp_path / "m.db"
    store = SQLiteTraceStore(db)
    store.upsert_trace(synthetic_trace)
    store.close()
    by_id = _serve(
        db,
        [_call_tool(1, "lens.cost", {"trace_id": synthetic_trace.trace_id})],
        capsys,
    )
    out = _payload(by_id[1])
    assert out["pricing_source"] == "file:prices.json"
    assert out["unpriced_models"] == []
    assert out["total_estimated_usd"] > 0


def test_bad_pricing_file_fails_fast_at_startup(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator-config error → exit 2 before serving, like app startup."""
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setenv("FORGE_LENS_PRICING_FILE", str(bad))
    db = tmp_path / "m.db"
    SQLiteTraceStore(db).close()
    stdin = io.StringIO(json.dumps(_rpc("tools/list", 1)) + "\n")
    old = sys.stdin
    sys.stdin = stdin
    try:
        rc = serve(str(db))
    finally:
        sys.stdin = old
    assert rc == 2
    assert "pricing" in capsys.readouterr().err


def test_failure_classification(
    seeded_db: Path,
    capsys: pytest.CaptureFixture[str],
    synthetic_trace: Trace,
) -> None:
    by_id = _serve(
        seeded_db,
        [
            _call_tool(
                1, "lens.failure", {"trace_id": synthetic_trace.trace_id}
            ),
            _call_tool(2, "lens.failure", {"trace_id": LOOP_TID}),
        ],
        capsys,
    )
    out = _payload(by_id[1])
    assert out["failed"] is True
    assert out["category"] == "timeout"
    assert out["evidence"]["span_ids"]
    clean = _payload(by_id[2])
    assert clean["failed"] is False
    assert clean["category"] == "unknown"


def test_list_datasets(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id = _serve(
        seeded_db,
        [
            _call_tool(1, "lens.list_datasets"),
            _call_tool(2, "lens.list_datasets", {"project_id": "other"}),
        ],
        capsys,
    )
    datasets = _payload(by_id[1])["datasets"]
    assert [d["dataset_id"] for d in datasets] == ["ds-1"]
    assert datasets[0]["name"] == "demo"
    assert _payload(by_id[2])["datasets"] == []


def test_capabilities_matrix(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id = _serve(seeded_db, [_call_tool(1, "lens.capabilities")], capsys)
    out = _payload(by_id[1])
    assert "claude_code" in out["hosts"]
    assert out["hosts"]["claude_code"]["native_tracing"] is False
    assert out["hosts"]["forge"]["native_tracing"] is True


def test_malformed_args_error_never_kills_loop(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Wrong-typed/shaped args are -32602 invalid params; a genuine
    internal fault is -32603. Either way the loop keeps serving."""
    by_id = _serve(
        seeded_db,
        [
            _call_tool(1, "lens.detect_loops", {"trace_id": 123}),
            _call_tool(
                2,
                "lens.failure",
                {"trace_id": "x" * 32, "project_id": 42},
            ),
            _call_tool(3, "lens.cost", "notadict"),
            _call_tool(4, "lens.trace_efficiency", {}),
            _call_tool(5, "lens.list_datasets", {"project_id": ["p"]}),
            _call_tool(6, "lens.list_traces", {"limit": "lots"}),
            _rpc("tools/list", 7),
        ],
        capsys,
    )
    for rid in range(1, 6):
        assert by_id[rid]["error"]["code"] == -32602, rid
    # "lots" passes the type check then fails int() — a real internal
    # ValueError, i.e. -32603, not invalid-params
    assert by_id[6]["error"]["code"] == -32603
    assert "tools" in by_id[7]["result"]


def test_ping_allowed_before_initialize(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """MCP lifecycle: ping is legal during the handshake — the client
    'SHOULD NOT send requests other than pings' before initialize."""
    stdin = io.StringIO(json.dumps(_rpc("ping", 42)) + "\n")
    old = sys.stdin
    sys.stdin = stdin
    try:
        serve(str(seeded_db))
    finally:
        sys.stdin = old
    out = [json.loads(line)
           for line in capsys.readouterr().out.splitlines() if line]
    assert out == [{"jsonrpc": "2.0", "id": 42, "result": {}}]


def test_internal_error_is_opaque_on_the_wire(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """-32603 carries the exception TYPE only — exception text can
    embed payload fragments/paths/secrets and never crosses."""
    secret = "sk-ant-oat01-SECRET-VALUE-9f8e7d6c"
    by_id = _serve(
        seeded_db,
        # a str limit passes the arg type check, then int() raises
        # ValueError whose message embeds the literal — the secret
        [_call_tool(1, "lens.list_traces", {"limit": secret})],
        capsys,
    )
    resp = by_id[1]
    assert resp["error"]["code"] == -32603
    assert resp["error"]["message"] == "internal tool error: ValueError"
    assert secret not in json.dumps(resp)


def test_unknown_tool_is_data_not_death(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    by_id = _serve(
        seeded_db,
        [_call_tool(1, "lens.nope", {}), _rpc("ping", 2)],
        capsys,
    )
    assert "unknown tool" in _payload(by_id[1])["error"]
    assert by_id[2]["result"] == {}


def test_sessions_tool_rolls_up(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two traces sharing session_id collapse to one roll-up row."""
    db_store = SQLiteTraceStore(seeded_db, readonly=False)
    base = _loop_trace()
    for i in range(2):
        t = base.model_copy(deep=True)
        t.trace_id = f"{'c' * 31}{i:x}"
        for s in t.spans:
            s.trace_id = t.trace_id
        t.session_id = "sess-shared"
        db_store.upsert_trace(t)
    db_store.close()

    by_id = _serve(
        seeded_db,
        [
            _call_tool(1, "lens.sessions", {"limit": 10}),
            _call_tool(2, "lens.sessions", {"limit": "x"}),
        ],
        capsys,
    )
    rows = _payload(by_id[1])["sessions"]
    shared = [r for r in rows if r["session_id"] == "sess-shared"]
    assert shared
    assert shared[0]["traces"] == 2
    assert set(shared[0]) == {
        "session_id", "traces", "spans",
        "first_started_at", "last_activity_at",
    }
    assert by_id[2]["error"]["code"] == -32603


def test_sessions_tools_offset_pages(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """offset walks both roll-ups without overlap; bad offset → error."""
    by_id = _serve(
        seeded_db,
        [
            _call_tool(1, "lens.sessions", {"limit": 1, "offset": 0}),
            _call_tool(2, "lens.sessions", {"limit": 1, "offset": 1}),
            _call_tool(3, "lens.tools", {"limit": 1, "offset": 0}),
            _call_tool(4, "lens.tools", {"limit": 1, "offset": 1}),
            _call_tool(5, "lens.sessions", {"offset": "x"}),
            _call_tool(6, "lens.tools", {"offset": True}),
        ],
        capsys,
    )
    s0 = _payload(by_id[1])["sessions"]
    s1 = _payload(by_id[2])["sessions"]
    if s1:
        assert s0[0]["session_id"] != s1[0]["session_id"]
    t0 = _payload(by_id[3])["tools"]
    t1 = _payload(by_id[4])["tools"]
    if t1:
        assert t0[0]["tool"] != t1[0]["tool"]
    # "x" fails int() — a real ValueError → -32603; a bool trips the
    # authored arg-type check → -32602 invalid params
    assert by_id[5]["error"]["code"] == -32603
    assert by_id[6]["error"]["code"] == -32602


def test_tools_tool_rolls_up(
    seeded_db: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """lens.tools mirrors /v1/tools — per-name fleet roll-up."""
    db_store = SQLiteTraceStore(seeded_db, readonly=False)
    t = _loop_trace()
    t.trace_id = "d" * 32
    for s in t.spans:
        s.trace_id = t.trace_id
        s.attributes = {"tool.name": "web_search"}
    db_store.upsert_trace(t)
    db_store.close()

    by_id = _serve(
        seeded_db,
        [_call_tool(1, "lens.tools", {}), _call_tool(2, "lens.tools", {"limit": "x"})],
        capsys,
    )
    rows = _payload(by_id[1])["tools"]
    ws = [r for r in rows if r["tool"] == "web_search"]
    assert ws
    assert ws[0]["calls"] >= 4  # seeded fixture may add more web_search calls
    assert by_id[2]["error"]["code"] == -32603
