"""Cycle 07 — loops, specialist runtime, alerts, ops summary."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forge_lens.api.app import create_app
from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.core.config import LensConfig
from forge_lens.ops.agents import AgentBudget, run_specialist
from forge_lens.ops.loops import detect_loops
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _tool(i: int, name: str = "read", *, error: bool = False,
          args: dict | None = None) -> Span:
    return Span(
        trace_id="ab" * 16, span_id=f"{i:016x}", name=name,
        kind=SpanKind.TOOL, start_time=_T0 + timedelta(milliseconds=i),
        end_time=_T0 + timedelta(milliseconds=i + 1),
        status=Status(code=StatusCode.ERROR if error else StatusCode.OK),
        attributes={"tool.args": args if args is not None else {"path": "/x"}},
    )


def _trace(spans: list[Span]) -> Trace:
    return Trace(trace_id="ab" * 16, root_span_id=spans[0].span_id,
                 started_at=_T0, ended_at=_T0, spans=spans,
                 status=Status(code=StatusCode.OK))


def test_detect_loops_repeated_identical() -> None:
    out = detect_loops(_trace([_tool(i) for i in range(4)]))
    kinds = {f["kind"] for f in out["findings"]}
    assert "repeated_identical_tool" in kinds
    assert out["classification"] == "heuristic"


def test_detect_loops_legit_pagination_not_flagged() -> None:
    spans = [_tool(i, name="poll", args={"cursor": i}) for i in range(5)]
    out = detect_loops(_trace(spans))
    assert not any(
        f["kind"] == "repeated_identical_tool" for f in out["findings"]
    )


def test_retry_loop_all_errors() -> None:
    kinds = {f["kind"] for f in
             detect_loops(_trace([_tool(i, error=True) for i in range(4)]))["findings"]}
    assert "retry_loop" in kinds


def test_specialist_known_unknown_and_jsonable(
    synthetic_trace: Trace,
) -> None:
    run = run_specialist("loop_detection_specialist", synthetic_trace,
                         budget=AgentBudget())
    assert run.stop_reason == "completed"
    assert run.findings
    json.dumps(run.findings)
    assert run_specialist("nope", synthetic_trace).stop_reason == (
        "unknown_specialist"
    )
    fr = run_specialist("failure_investigator", synthetic_trace)
    json.dumps(fr.findings)


def test_specialist_cancel_flag() -> None:
    run = run_specialist("trace_investigator", _trace([_tool(0)]))
    run.cancel()
    assert run._cancel


@pytest.fixture
def client(tmp_path: Path):
    store = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db")
    app = create_app(store=store, config=cfg)
    with TestClient(app) as c:
        yield c
    store.close()


def test_alert_evaluate_endpoint(client: TestClient) -> None:
    r = client.post("/v1/alerts/evaluate", json={"rules": [
        {"rule_id": "r1", "metric": "trace_count", "op": ">", "threshold": -1},
        {"rule_id": "r2", "metric": "error_rate", "op": ">", "threshold": 0.99},
    ]})
    assert r.status_code == 200
    assert {f["rule_id"] for f in r.json()["fired"]} == {"r1"}
    r_bad = client.post("/v1/alerts/evaluate", json={"rules": "x"})
    assert r_bad.status_code == 422


def test_ops_summary_and_specialists_endpoints(client: TestClient) -> None:
    r = client.get("/v1/ops/summary")
    assert r.status_code == 200
    assert "stats" in r.json()
    r2 = client.get("/v1/analyze/specialists")
    assert "loop_detection_specialist" in r2.json()["specialists"]


def test_analyze_endpoint_runs_specialist(
    client: TestClient, synthetic_trace: Trace,
) -> None:
    env = {"traces": [synthetic_trace.model_dump(mode="json")]}
    assert client.post("/v1/traces", json=env).status_code == 200
    r = client.post(
        f"/v1/analyze/loop_detection_specialist/{synthetic_trace.trace_id}",
        json={},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stop_reason"] == "completed"
    assert body["findings"]
    r404 = client.post("/v1/analyze/trace_investigator/deadbeef", json={})
    assert r404.status_code == 404


def test_delete_trace_ids_exact_scope(tmp_path: Path) -> None:
    """P0: archive deletes only exported ids — never a re-computed set."""
    from forge_lens.storage.sqlite import SQLiteTraceStore

    store = SQLiteTraceStore(tmp_path / "d.db")
    kept = _trace([_tool(0)])
    gone = _trace([_tool(1)])
    # different trace ids
    gone2 = Trace(trace_id="cd" * 16, root_span_id=gone.spans[0].span_id,
                  started_at=_T0, ended_at=_T0, spans=gone.spans,
                  status=Status(code=StatusCode.OK))
    for s in gone2.spans:
        object.__setattr__(s, "trace_id", "cd" * 16)
    store.upsert_trace(kept)
    store.upsert_trace(gone2)
    out = store.delete_trace_ids([gone2.trace_id])
    assert out["traces"] == 1
    assert store.get_trace(gone2.trace_id) is None
    assert store.get_trace(kept.trace_id) is not None
    # project-scoped delete misses when project mismatches
    out2 = store.delete_trace_ids([kept.trace_id], project_id="other")
    assert out2["traces"] == 0
    store.close()


def test_claude_code_adapter_any_session_id(tmp_path: Path) -> None:
    """Non-hex / missing sessionIds must not crash or break the pattern."""
    import json

    from forge_lens.adapters.claude_code import parse_session

    f = tmp_path / "sess.jsonl"
    f.write_text(json.dumps({
        "type": "tool_use", "tool_name": "read",
        "sessionId": "not-hex-🎉-short",
        "timestamp": "2026-01-01T10:00:00Z"}) + "\n")
    t = parse_session(f)
    assert t is not None
    assert len(t.trace_id) == 32
    int(t.trace_id, 16)  # hex-valid
    # no sessionId at all → filename-derived, still valid
    f2 = tmp_path / "plain-file.jsonl"
    f2.write_text(json.dumps({"tool_name": "x", "is_error": True}) + "\n")
    t2 = parse_session(f2)
    assert t2 is not None
    assert len(t2.trace_id) == 32
    assert t2.status.code is StatusCode.ERROR
    # garbage lines + no tool events → None, not a fabricated trace
    f3 = tmp_path / "empty.jsonl"
    f3.write_text("not json\n{}\n")
    assert parse_session(f3) is None


def test_mcp_server_survives_bad_call(tmp_path: Path, capsys) -> None:
    """One malformed tools/call → JSON-RPC error, loop keeps serving."""
    import io
    import json
    import sys

    from forge_lens.mcp_server import serve

    store_path = tmp_path / "m.db"
    store = SQLiteTraceStore(store_path)
    store.close()
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "lens.get_trace", "arguments": "notadict"}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "lens.get_trace", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 5, "method": "bogus"},
    ]
    stdin = io.StringIO("\n".join(json.dumps(m) for m in msgs) + "\n")
    old = sys.stdin
    sys.stdin = stdin
    try:
        serve(str(store_path))
    finally:
        sys.stdin = old
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    by_id = {o["id"]: o for o in out}
    assert "result" in by_id[1]
    assert by_id[2]["error"]["code"] == -32603
    assert by_id[3]["error"]["code"] == -32603
    assert "tools" in by_id[4]["result"]
    assert by_id[5]["error"]["code"] == -32601


def test_claude_code_session_id_before_first_tool(tmp_path: Path) -> None:
    """Regression: tool_use BEFORE the sessionId line must still share
    the resolved trace id (two-pass parse), and session_id lands on
    the Trace for session grouping."""
    import json as _json

    from forge_lens.adapters.claude_code import parse_session

    f = tmp_path / "s.jsonl"
    f.write_text(
        _json.dumps({"type": "tool_use", "tool_name": "early",
                     "timestamp": "2026-01-01T10:00:00Z"})
        + "\n"
        + _json.dumps({"sessionId": "abc-non-hex", "type": "meta"})
        + "\n"
        + _json.dumps({"type": "tool_use", "tool_name": "late",
                       "timestamp": "2026-01-01T10:00:01Z"})
        + "\n"
    )
    t = parse_session(f)
    assert t is not None
    assert t.session_id == "abc-non-hex"
    assert {s.trace_id for s in t.spans} == {t.trace_id}


def test_alerts_corrupt_rules_file(tmp_path: Path) -> None:
    """Corrupt/junk rules file → tolerant load, save still repairs."""
    from forge_lens.ops.alerts import delete_rule, load_rules, save_rule

    p = tmp_path / "alert-rules.json"
    p.write_text('{"rules": [{"rule_id": 1}, "junk", {"rule_id": "ok"}')
    assert load_rules(p) == []  # truncated JSON → []
    p.write_text('{"rules": [{"rule_id": 1}, "junk", {"rule_id": "good", '
                 '"metric": "trace_count", "op": ">", "threshold": 0}]}')
    assert [r["rule_id"] for r in load_rules(p)] == ["good"]
    out = save_rule(p, {"rule_id": "b", "metric": "error_rate",
                        "op": ">", "threshold": 0.5})
    assert [r["rule_id"] for r in out] == ["b", "good"]
    assert [r["rule_id"] for r in delete_rule(p, "good")] == ["b"]


def test_delete_trace_ids_shared_id_across_projects(tmp_path: Path) -> None:
    """A trace_id present in two projects: unscoped delete removes all
    rows; scoped delete removes only its project's."""
    store = SQLiteTraceStore(tmp_path / "m.db")
    for proj in ("a", "b"):
        tr = _trace([_tool(0)])
        t2 = Trace(trace_id="ef" * 16, root_span_id=tr.spans[0].span_id,
                   started_at=_T0, ended_at=_T0, spans=tr.spans,
                   project_id=proj, status=Status(code=StatusCode.OK))
        for s in t2.spans:
            object.__setattr__(s, "trace_id", "ef" * 16)
        store.upsert_trace(t2)
    out = store.delete_trace_ids(["ef" * 16], project_id="a")
    assert out["traces"] == 1
    assert store.get_trace("ef" * 16, project_id="a") is None
    assert store.get_trace("ef" * 16, project_id="b") is not None
    assert store.delete_trace_ids(["ef" * 16])["traces"] == 1
    store.close()


def test_archive_cli_deletes_only_exported(tmp_path: Path) -> None:
    """P0 regression at the CLI surface: --delete removes exactly the
    written ids; a started-before/ended-after trace survives."""
    from typer.testing import CliRunner

    from forge_lens.cli.main import app

    db = tmp_path / "a.db"
    store = SQLiteTraceStore(db)
    old = Trace(trace_id="aa" * 16, root_span_id="0" * 16,
                started_at=_T0, ended_at=_T0, spans=[_tool(0)],
                status=Status(code=StatusCode.OK))
    for s in old.spans:
        object.__setattr__(s, "trace_id", "aa" * 16)
    # started before cutoff, ends AFTER it — must not be deleted
    straddler = Trace(trace_id="bb" * 16, root_span_id="0" * 16,
                      started_at=_T0, ended_at=_T0 + timedelta(days=30),
                      spans=[_tool(1)], status=Status(code=StatusCode.OK))
    for s in straddler.spans:
        object.__setattr__(s, "trace_id", "bb" * 16)
    store.upsert_trace(old)
    store.upsert_trace(straddler)
    store.close()

    arc = tmp_path / "arch.ndjson"
    r = CliRunner().invoke(app, [
        "archive", "--db", str(db),
        "--before", (_T0 + timedelta(days=1)).isoformat(),
        "--out", str(arc), "--delete", "--yes"])
    assert r.exit_code == 0, r.output
    assert "archived 1" in r.output
    assert "pruned 1" in r.output
    store = SQLiteTraceStore(db)
    assert store.get_trace("aa" * 16) is None
    assert store.get_trace("bb" * 16) is not None  # survived — never archived
    store.close()
    # rerun to same path without --append → refused
    r2 = CliRunner().invoke(app, [
        "archive", "--db", str(db),
        "--before", (_T0 + timedelta(days=1)).isoformat(),
        "--out", str(arc)])
    assert r2.exit_code == 2


def test_archive_cli_cross_project(tmp_path: Path) -> None:
    """Same trace_id under two projects, --project omitted: each copy
    must be exported under its own project and both deleted — never
    export one copy twice then delete both."""
    import json as _json

    from typer.testing import CliRunner

    from forge_lens.cli.main import app

    db = tmp_path / "x.db"
    store = SQLiteTraceStore(db)
    for proj in ("p1", "p2"):
        tr = _trace([_tool(0)])
        t2 = Trace(trace_id="cc" * 16, root_span_id=tr.spans[0].span_id,
                   started_at=_T0, ended_at=_T0, spans=tr.spans,
                   project_id=proj, status=Status(code=StatusCode.OK))
        for s in t2.spans:
            object.__setattr__(s, "trace_id", "cc" * 16)
        store.upsert_trace(t2)
    store.close()

    arc = tmp_path / "both.ndjson"
    r = CliRunner().invoke(app, [
        "archive", "--db", str(db),
        "--before", (_T0 + timedelta(days=1)).isoformat(),
        "--out", str(arc), "--delete", "--yes"])
    assert r.exit_code == 0, r.output
    lines = [ln for ln in arc.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2
    assert {_json.loads(ln)["project_id"] for ln in lines} == {"p1", "p2"}
    store = SQLiteTraceStore(db)
    assert store.get_trace("cc" * 16, project_id="p1") is None
    assert store.get_trace("cc" * 16, project_id="p2") is None
    store.close()


def test_parse_session_project_id_lands(tmp_path: Path) -> None:
    """import-claude --project must actually land in that project."""
    import json as _json

    from forge_lens.adapters.claude_code import parse_session

    f = tmp_path / "s.jsonl"
    f.write_text(_json.dumps(
        {"sessionId": "s1", "type": "tool_use", "tool_name": "x",
         "timestamp": "2026-01-01T10:00:00Z"}) + "\n")
    t = parse_session(f, project_id="my-proj")
    assert t is not None
    assert t.project_id == "my-proj"
