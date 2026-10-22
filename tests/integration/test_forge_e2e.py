"""E2E: the-forge → Forge Lens com dados reais (nada sintético).

Fluxo exercitado — o mesmo do harness ``examples/forge-integration/``:

1. API Forge Lens real (uvicorn em thread, socket livre, SQLite em tmp_path).
2. Um run REAL de ``theforge ask`` por cenário quando o sibling está
   disponível (``.venv/bin/theforge`` ou ``python -m theforge`` no checkout
   irmão, ou ``theforge`` no PATH): echo-forge (ok) + provider fixture que
   quebra de verdade (``provider_failure``).
3. Quando o sibling não existe no ambiente, o teste cai para os artefatos
   REAIS gravados em ``examples/forge-integration/fixtures/captured/``
   (produzidos por runs reais e sanitizados — nunca sintéticos). Os spans
   importados continuam reais: medidos pelo recorder do theforge.
4. Assertions: spans persistidos, queryáveis via store/REST/MCP stdio, e a
   trace de falha classificada como ``tool_error`` por ``classify_failure``.

Para rodar: ``.venv/bin/python -m pytest tests/integration/test_forge_e2e.py -xvs``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "forge-integration"
sys.path.insert(0, str(_EXAMPLES))

import forge_bridge as fb  # noqa: E402

from forge_lens.contracts.provenance import Provenance  # noqa: E402
from forge_lens.contracts.v1.trace import StatusCode  # noqa: E402
from forge_lens.investigate import classify_failure  # noqa: E402
from forge_lens.sdk.exporter import HTTPExporter  # noqa: E402
from forge_lens.sdk.tracer import LensTracer  # noqa: E402

OK_INTENT = "echo hello from the forge-lens e2e test"
FAIL_INTENT = "explode the fixture"


def _mode() -> str:
    return "live" if fb.find_theforge() else "captured"


@pytest.fixture(scope="module")
def flow(tmp_path_factory: pytest.TempPathFactory) -> dict:
    """Um run do fluxo inteiro por módulo (dois `theforge ask` reais ~30s)."""
    data_dir = tmp_path_factory.mktemp("forge-e2e")
    results: dict = {"mode": _mode()}

    with fb.lens_server(data_dir) as srv:
        results["endpoint"] = srv["endpoint"]
        exporter = HTTPExporter(srv["endpoint"], project_id=fb.PROJECT_ID)
        tracer = LensTracer(
            service_name="forge-integration-test",
            exporter=exporter,
            session_id="forge-integration-e2e",
            project_id=fb.PROJECT_ID,
            auto_flush=False,  # flush() explícito: spans importados levam os
            # tempos reais corrigidos depois do `with` do root
            register_atexit=False,
        )
        runs: list[dict] = []
        if results["mode"] == "live":
            theforge = fb.find_theforge()
            assert theforge is not None
            ws = data_dir / "tf-workspace"
            ws.mkdir()
            env = fb.theforge_env(ws)
            fb.init_workspace(theforge, ws, env)
            runs.append(fb.traced_theforge_ask(
                tracer, theforge, ws, env,
                intent=OK_INTENT, use=fb.OK_PROVIDER_ID,
                label="ask echo-forge"))
            runs.append(fb.traced_theforge_ask(
                tracer, theforge, ws, env,
                intent=FAIL_INTENT, use=fb.FAIL_PROVIDER_ID,
                capability=fb.FAIL_CAPABILITY, allow_unverified=True,
                label="ask failing-forge"))
        else:
            for name in ("ok", "fail"):
                runs.append(fb.import_captured_run(
                    tracer, fb.CAPTURED_DIR / name))
        tracer.flush()
        results["runs"] = runs
        results["db"] = srv["db"]
        results["store"] = srv["store"]
        results["endpoint"] = srv["endpoint"]
        # o servidor cai ao sair do contexto — o store/db ficam legíveis
        # (readonly) para as asserções offline; REST/MCP acontecem dentro.
        items = srv["store"].list_traces(project_id=fb.PROJECT_ID)
        results["stored_items"] = items
        results["traces"] = {
            i["trace_id"]: srv["store"].get_trace(
                i["trace_id"], project_id=fb.PROJECT_ID)
            for i in items
        }
        results["fail_trace_id"] = next(
            (tid for tid, t in results["traces"].items()
             if t and any(
                 s.status.code.value == "error"
                 and "failing-forge" in json.dumps(s.attributes)
                 for s in t.spans)),
            None,
        )
        results["fail_report"] = (
            classify_failure(results["traces"][results["fail_trace_id"]])
            if results["fail_trace_id"] else None
        )
        # MCP stdio real: o servidor lê o mesmo db em modo readonly
        with fb.McpClient(results["db"], cwd=data_dir) as mcp:
            results["mcp_init"] = mcp.request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "e2e-test", "version": "0"},
            })
            mcp.notify("notifications/initialized")
            results["mcp_tools"] = mcp.request("tools/list")
            results["mcp_list"] = mcp.call_tool(
                "lens.list_traces", {"project_id": fb.PROJECT_ID})
            if results["fail_trace_id"]:
                results["mcp_failure"] = mcp.call_tool(
                    "lens.failure",
                    {"trace_id": results["fail_trace_id"],
                     "project_id": fb.PROJECT_ID})
        # REST real sobre o socket vivo (ainda dentro do with)
        results["rest_list"] = fb.http_get(
            f"{srv['endpoint']}/v1/traces?project_id={fb.PROJECT_ID}")
        if results["fail_trace_id"]:
            results["rest_failure"] = fb.http_get(
                f"{srv['endpoint']}/v1/traces/{results['fail_trace_id']}"
                f"/failure?project_id={fb.PROJECT_ID}")
    return results


def test_runs_produced_real_spans(flow: dict) -> None:
    assert len(flow["runs"]) == 2
    for run in flow["runs"]:
        assert run["imported_spans"] >= 3, run
        assert run["run_id"]
    statuses = {r["run_status"] for r in flow["runs"]}
    if flow["mode"] == "live":
        assert statuses == {"ok", "provider_failure"}, statuses
    else:  # captured: replay dos status reais gravados
        assert statuses == {"ok", "provider_failure"}, statuses


def test_spans_persisted_and_queryable(flow: dict) -> None:
    items = flow["stored_items"]
    assert len(items) == 2
    ok_items = [i for i in items if i["status"] == "ok"]
    err_items = [i for i in items if i["status"] == "error"]
    assert len(ok_items) == 1
    assert len(err_items) == 1
    traces = flow["traces"]
    for t in traces.values():
        assert t is not None
        # 1 root do harness + N spans importados do theforge
        assert len(t.spans) >= 4
        imported = [s for s in t.spans
                    if s.provenance == Provenance.IMPORTED]
        assert len(imported) >= 3
        assert all(s.end_time is not None for s in t.spans)
        names = {s.name for s in t.spans}
        assert any(n.startswith("theforge.") or n.startswith("provider:")
                   for n in names), names


def test_rest_reads_over_real_socket(flow: dict) -> None:
    body = flow["rest_list"]
    assert len(body["items"]) == 2
    rep = flow["rest_failure"]
    assert rep["failed"] is True
    assert rep["category"] == "tool_error"


def test_failure_trace_classified(flow: dict) -> None:
    assert flow["fail_trace_id"] is not None
    rep = flow["fail_report"]
    assert rep is not None
    assert rep.failed is True
    # span provider:failing-forge é TOOL + error → tool_error, observed
    assert rep.category.value == "tool_error"
    assert rep.confidence == "observed"


def test_mcp_stdio_round_trip(flow: dict) -> None:
    init = flow["mcp_init"]["result"]
    assert init["serverInfo"]["name"] == "forge-lens"
    tools = [t["name"] for t in flow["mcp_tools"]["result"]["tools"]]
    assert "lens.failure" in tools
    assert "lens.list_traces" in tools
    listed = flow["mcp_list"]["payload"]["traces"]
    assert len(listed) == 2
    fail = flow["mcp_failure"]["payload"]
    assert fail["failed"] is True
    assert fail["category"] == "tool_error"


def test_captured_fixtures_import_offline() -> None:
    """O caminho de fallback (sibling ausente) é exercido sempre — fixtures
    são artefatos REAIS gravados de runs anteriores, nunca sintéticos."""
    tracer = LensTracer(service_name="captured-check", auto_flush=False,
                        register_atexit=False)
    summaries = [
        fb.import_captured_run(tracer, fb.CAPTURED_DIR / name)
        for name in ("ok", "fail")
    ]
    assert tracer.flush() is not None
    traces = tracer.exporter.traces
    assert len(traces) == 2
    assert [s["run_status"] for s in summaries] == ["ok", "provider_failure"]
    for t in traces:
        # replay honesto: até o root sai como imported
        assert all(s.provenance == Provenance.IMPORTED for s in t.spans)
    fail_t = traces[1]
    rep = classify_failure(fail_t)
    assert rep.failed is True
    assert rep.category.value == "tool_error"


def test_error_span_carries_real_theforge_evidence(flow: dict) -> None:
    t = flow["traces"][flow["fail_trace_id"]]
    # the provider span specifically — in captured mode the ROOT span is
    # also IMPORTED+ERROR (its own run-status replay), so a bare
    # "first imported error" pick lands on the root, which carries
    # theforge.run_id evidence but no theforge.span.name
    err = next(s for s in t.spans if s.status.code == StatusCode.ERROR
               and s.provenance == Provenance.IMPORTED
               and s.attributes.get("theforge.span.name")
               == "provider:failing-forge")
    assert err.attributes["theforge.span.name"] == "provider:failing-forge"
    assert err.attributes["error.type"] == "FORGE-PROTO-EXIT"
    assert "exit code 3" in err.attributes["error.message"]
    # duração importada é a medida real do theforge, não um placeholder
    assert err.attributes["theforge.duration_ms"] > 0
    assert err.duration_ms == pytest.approx(
        float(err.attributes["theforge.duration_ms"]), abs=1.0)
