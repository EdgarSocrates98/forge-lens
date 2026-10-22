"""LensClient — the read-side SDK against a real uvicorn on loopback.

The client is a network client: TestClient cannot reach it. Live
behavior is exercised over a real socket (same pattern as
``tests/e2e/test_full_flow.py::test_http_exporter_over_real_socket``);
request shapes and error paths use ``patch("urllib.request.urlopen")``
(same pattern as ``test_exporter_body_shape``).

Marked ``network`` — the module legitimately opens loopback sockets, so
the ``_no_network`` offline gate in ``tests/conftest.py`` skips it.
"""

from __future__ import annotations

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
from typing import Any
from unittest.mock import patch

import pytest
import uvicorn

from forge_lens.api.app import create_app
from forge_lens.contracts.v1.trace import Trace
from forge_lens.core.config import LensConfig
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.sdk.query import LensClient, QueryError
from forge_lens.storage.sqlite import SQLiteTraceStore

pytestmark = pytest.mark.network


def _serve(app: Any) -> tuple[str, Any, threading.Thread]:
    """Bind uvicorn to an ephemeral loopback port and wait for startup."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    return f"http://127.0.0.1:{port}", server, thread


@pytest.fixture
def live(tmp_path, synthetic_trace: Trace):
    """Real API on a loopback port, pre-loaded with one trace + dataset."""
    db = tmp_path / "live.db"
    store = SQLiteTraceStore(db)
    cfg = LensConfig(data_dir=tmp_path, db_path=db)
    synthetic_trace.session_id = "sdk-q-sess"
    IngestPipeline(store, cfg).ingest([synthetic_trace])
    store.upsert_dataset("ds-1", project_id="default", name="eval set")
    store.upsert_dataset_version(
        "ds-1",
        "v1",
        project_id="default",
        cases=[{"case_id": "c1", "input": {"q": "x"}, "expected": {}}],
    )
    url, server, thread = _serve(create_app(store=store, config=cfg))
    try:
        yield url, synthetic_trace.trace_id
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        store.close()


@pytest.fixture
def client(live) -> LensClient:
    return LensClient(live[0])


# -- live round-trips -----------------------------------------------------


def test_list_traces_envelope_and_filters(
    client: LensClient, live: tuple[str, str]
) -> None:
    _, tid = live
    out = client.list_traces()
    assert {*out} >= {"items", "limit", "offset"}
    assert [i["trace_id"] for i in out["items"]] == [tid]
    # server-side filters reach the store: the seeded trace is an error
    assert len(client.list_traces(status="error")["items"]) == 1
    assert client.list_traces(status="ok")["items"] == []
    assert len(client.list_traces(session_id="sdk-q-sess")["items"]) == 1
    assert client.list_traces(q="agent.run")["items"]
    assert client.list_traces(tool="web_search")["items"]
    # a project mismatch honestly returns nothing — never server data
    assert client.list_traces(project_id="other-project")["items"] == []


def test_get_trace_and_404(client: LensClient, live: tuple[str, str]) -> None:
    _, tid = live
    trace = client.get_trace(tid)
    assert trace["trace_id"] == tid
    assert len(trace["spans"]) == 3
    with pytest.raises(QueryError) as excinfo:
        client.get_trace("f" * 32)
    assert excinfo.value.status_code == 404


def test_list_sessions_and_tools(client: LensClient) -> None:
    sessions = client.list_sessions()
    assert sessions["items"][0]["session_id"] == "sdk-q-sess"
    assert sessions["items"][0]["traces"] == 1
    tools = client.list_tools()
    assert {t["tool"] for t in tools["items"]} == {"web_search"}
    assert tools["items"][0]["errors"] == 1


def test_trace_intelligence_routes(
    client: LensClient, live: tuple[str, str]
) -> None:
    _, tid = live
    cost = client.trace_cost(tid)
    assert cost["total_tokens"] == 142
    assert cost["pricing_source"]
    assert isinstance(cost["waste"], list)

    failure = client.trace_failure(tid)
    assert failure["failed"] is True
    # the seeded tool span's status message is "timeout" — timeout is
    # the most specific category and wins over tool_error
    assert failure["category"] == "timeout"

    eff = client.trace_efficiency(tid)
    assert eff["tool_calls"] == 1
    assert eff["llm_calls"] == 1

    ev = client.trace_eval(tid)
    assert ev["trace_id"] == tid
    assert {r["evaluator"] for r in ev["results"]} == {
        "error_free",
        "tool_success",
    }

    loops = client.trace_loops(tid)
    assert loops["trace_id"] == tid
    assert isinstance(loops["findings"], list)

    recs = client.trace_recommendations(tid)
    assert recs["trace_id"] == tid
    assert isinstance(recs["recommendations"], list)


def test_trace_graph_routes(client: LensClient, live: tuple[str, str]) -> None:
    _, tid = live
    graph = client.trace_graph(tid)
    assert graph["stats"]["span_count"] == 3

    cp = client.trace_critical_path(tid)
    assert cp["path"]

    bottlenecks = client.trace_bottlenecks(tid, top=2)
    assert len(bottlenecks["items"]) == 2

    analysis = client.trace_graph_analysis(tid)
    assert analysis["nodes"] == 3

    sg = client.session_graph("sdk-q-sess")
    assert sg["stats"]["truncated"] is False


def test_trace_investigation_routes(
    client: LensClient, live: tuple[str, str]
) -> None:
    _, tid = live
    replay = client.trace_replay(tid)
    assert replay["span_count"] == 3
    assert replay["entries"]

    incident = client.trace_incident(tid)
    assert incident["bundle_version"] == "1.0.0"
    assert incident["failure"]["failed"] is True

    security = client.trace_security(tid)
    assert security["mode"] == "observer"


def test_datasets_bare_arrays(client: LensClient) -> None:
    datasets = client.list_datasets()
    assert isinstance(datasets, list)  # no items-envelope on this route
    assert [d["dataset_id"] for d in datasets] == ["ds-1"]

    ds = client.get_dataset("ds-1")
    assert ds["name"] == "eval set"

    versions = client.list_dataset_versions("ds-1")
    assert [v["version"] for v in versions] == ["v1"]
    assert versions[0]["case_count"] == 1

    version = client.get_dataset_version("ds-1", "v1")
    assert version["cases"][0]["case_id"] == "c1"

    with pytest.raises(QueryError) as excinfo:
        client.get_dataset("missing")
    assert excinfo.value.status_code == 404


def test_meta_routes(client: LensClient) -> None:
    health = client.health()
    assert health["status"] == "ok"
    assert health["stats"]["traces"] == 1

    assert client.metrics()["counters"]["lens.api.requests"] > 0
    assert "hosts" in client.capabilities()
    assert client.pricing()["models"]

    summary = client.ops_summary()
    assert summary["project_id"] == "default"
    assert summary["recent_failures"]  # the seeded trace errored


def test_read_side_post_routes(
    client: LensClient, live: tuple[str, str]
) -> None:
    _, tid = live
    report = client.eval_run([tid], [{"name": "error_free"}])
    # EvalReport.results is keyed by trace_id
    assert report["results"][tid][0]["evaluator"] == "error_free"
    assert report["results"][tid][0]["score"] == 0.0  # fixture errored
    assert "summary" in report

    compare = client.eval_compare(
        {"trace_ids": [tid]},
        {"trace_ids": [tid]},
        [{"name": "error_free"}],
        gate={"metrics": {"error_free/no_error_spans": {"min_pass_rate_b": 0.0}}},
    )
    assert compare["deltas"]
    assert compare["gate"]["gate_passed"] is True

    fleet = client.fleet_efficiency([tid])
    assert fleet["traces"] == 1
    assert fleet["succeeded"] == 0

    cohorts = client.compare_cohorts({"trace_ids": [tid]}, {"trace_ids": [tid]})
    assert cohorts["classification"] == "observed_difference"
    assert cohorts["base_traces"] == 1


def test_specialists_and_alerts(client: LensClient, live: tuple[str, str]) -> None:
    _, tid = live
    specialists = client.list_specialists()
    assert "trace_investigator" in specialists["specialists"]

    run = client.run_analysis("trace_investigator", tid)
    assert run["specialist"] == "trace_investigator"
    assert run["stop_reason"] == "completed"
    assert run["findings"]

    fired = client.alerts_evaluate(
        [{"rule_id": "has-errors", "metric": "error_count",
          "op": ">=", "threshold": 1}]
    )
    assert fired["evaluated"] == 1
    assert fired["fired"][0]["rule_id"] == "has-errors"


def test_auth_reader_key_required(tmp_path, synthetic_trace: Trace) -> None:
    """auth_reads + api_key: a reader call without the key is a 401;
    the same key in the client unlocks every read route."""
    db = tmp_path / "auth.db"
    store = SQLiteTraceStore(db)
    cfg = LensConfig(
        data_dir=tmp_path, db_path=db, api_key="reader-key", auth_reads=True
    )
    app = create_app(store=store, config=cfg)
    url, server, thread = _serve(app)
    try:
        with pytest.raises(QueryError) as excinfo:
            LensClient(url).health()
        assert excinfo.value.status_code == 401
        assert LensClient(url, api_key="reader-key").health()["status"] == "ok"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        store.close()


# -- request shape via patched transport ----------------------------------


class _FakeResp:
    def __init__(self, payload: bytes = b'{"items": [], "limit": 50, "offset": 0}'):
        self._payload = payload

    def __enter__(self) -> _FakeResp:
        return self

    def __exit__(self, *a: Any) -> bool:
        return False

    def read(self) -> bytes:
        return self._payload


def _capture(payload: bytes = b'{"items": [], "limit": 50, "offset": 0}'):
    captured: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: Any = None) -> _FakeResp:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["key"] = req.headers.get("X-forge-lens-key")
        captured["body"] = json.loads(req.data) if req.data else None
        return _FakeResp(payload)

    return captured, fake_urlopen


def test_request_encodes_filters_and_key_header() -> None:
    captured, fake = _capture()
    client = LensClient("http://lens.test", api_key="k", project_id="acme")
    with patch("urllib.request.urlopen", fake):
        out = client.list_traces(
            session_id="s 1",
            status="error",
            since="2026-01-01T00:00:00Z",
            min_duration_ms=12.5,
            q="x",
            tool="web",
            limit=10,
            offset=5,
        )
    assert out == {"items": [], "limit": 50, "offset": 0}
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert captured["url"].startswith("http://lens.test/v1/traces?")
    assert params["project_id"] == ["acme"]
    assert params["session_id"] == ["s 1"]
    assert params["status"] == ["error"]
    assert params["since"] == ["2026-01-01T00:00:00Z"]
    assert params["min_duration_ms"] == ["12.5"]
    assert params["limit"] == ["10"]
    assert params["offset"] == ["5"]
    assert captured["key"] == "k"
    assert captured["method"] == "GET"


def test_project_id_client_default_and_per_call_override() -> None:
    captured, fake = _capture()
    client = LensClient("http://lens.test", project_id="cfg-proj")
    with patch("urllib.request.urlopen", fake):
        client.get_trace("t" * 32)
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert params["project_id"] == ["cfg-proj"]
    with patch("urllib.request.urlopen", fake):
        client.get_trace("t" * 32, project_id="other")
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert params["project_id"] == ["other"]


def test_datetime_filters_serialize_iso() -> None:
    from datetime import datetime

    captured, fake = _capture()
    client = LensClient("http://lens.test")
    with patch("urllib.request.urlopen", fake):
        # naive datetimes read as UTC — the server's own convention
        client.list_traces(since=datetime(2026, 1, 1, 12, 0, 0))  # noqa: DTZ001
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(captured["url"]).query)
    assert params["since"] == ["2026-01-01T12:00:00+00:00"]


def test_path_segments_are_url_quoted() -> None:
    captured, fake = _capture()
    client = LensClient("http://lens.test")
    with patch("urllib.request.urlopen", fake):
        client.get_dataset("ds/a b")
    assert captured["url"].endswith("/v1/datasets/ds%2Fa%20b?project_id=default")


def test_post_body_shape() -> None:
    captured, fake = _capture()
    client = LensClient("http://lens.test", project_id="acme")
    with patch("urllib.request.urlopen", fake):
        client.eval_run(["t1"], [{"name": "error_free"}], dataset="ds-1")
    assert captured["method"] == "POST"
    assert captured["url"] == "http://lens.test/v1/eval/run"
    assert captured["body"] == {
        "trace_ids": ["t1"],
        "evaluators": [{"name": "error_free"}],
        "dataset": "ds-1",
        "project_id": "acme",
    }
    with patch("urllib.request.urlopen", fake):
        client.fleet_efficiency()  # omitted trace_ids stays out of the body
    assert captured["body"] == {"project_id": "acme"}


# -- failure modes → one clean exception ----------------------------------


def test_http_error_carries_status_and_detail() -> None:
    def boom(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.HTTPError(
            req.full_url, 403, "forbidden",
            hdrs=None, fp=io.BytesIO(b'{"detail": "scope"}'),
        )

    with (
        patch("urllib.request.urlopen", boom),
        pytest.raises(QueryError) as excinfo,
    ):
        LensClient("http://lens.test").health()
    assert excinfo.value.status_code == 403


def test_transport_error_has_no_status() -> None:
    def boom(req: Any, timeout: Any = None) -> Any:
        raise urllib.error.URLError("connection refused")

    with (
        patch("urllib.request.urlopen", boom),
        pytest.raises(QueryError) as excinfo,
    ):
        LensClient("http://lens.test").health()
    assert excinfo.value.status_code is None
    assert "connection refused" in str(excinfo.value)


def test_bare_oserror_is_wrapped() -> None:
    def boom(req: Any, timeout: Any = None) -> Any:
        raise TimeoutError("timed out")

    with patch("urllib.request.urlopen", boom), pytest.raises(QueryError):
        LensClient("http://lens.test").health()


def test_non_json_response_is_query_error() -> None:
    _, fake = _capture(payload=b"<html>proxy error</html>")
    with patch("urllib.request.urlopen", fake), pytest.raises(QueryError):
        LensClient("http://lens.test").health()


# -- config resolution -----------------------------------------------------


def test_from_config_resolves_env(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """from_config() follows the same env+config.toml chain as the CLI."""
    monkeypatch.chdir(tmp_path)  # no .forge-lens/config.toml here
    monkeypatch.setenv("FORGE_LENS_ENDPOINT", "http://lens.example:9000")
    monkeypatch.setenv("FORGE_LENS_API_KEY", "flk_sk_test")
    monkeypatch.setenv("FORGE_LENS_PROJECT", "acme")
    client = LensClient.from_config()
    assert client.endpoint == "http://lens.example:9000"
    assert client.api_key == "flk_sk_test"
    assert client.project_id == "acme"


def test_from_config_accepts_explicit_config(tmp_path) -> None:
    cfg = LensConfig(
        data_dir=tmp_path, endpoint="http://e:1", api_key="k", project_id="p"
    )
    client = LensClient.from_config(cfg)
    assert (client.endpoint, client.api_key, client.project_id) == (
        "http://e:1",
        "k",
        "p",
    )
