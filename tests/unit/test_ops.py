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
