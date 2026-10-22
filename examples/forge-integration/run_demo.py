#!/usr/bin/env python3
"""Demo E2E real: the-forge → Forge Lens.

O que acontece (tudo real, offline, nada fabricado):

1. Sobe a API Forge Lens (uvicorn em thread, porta livre, SQLite em
   ``<data-dir>/.forge-lens/lens.db``).
2. ``theforge init`` + ``theforge ask`` num workspace temporário:
     - run OK   — provider builtin ``echo-forge`` (intenção "echo ...")
     - run FAIL — provider fixture ``failing-forge`` que declara
       ``demo.failcrash`` no describe e quebra de verdade no execute
       (exit 3) → ``receipt.status = provider_failure``.
3. Cada run vira uma Trace: root span = subprocess medido pelo harness
   (OBSERVED); spans internos = ``theforge/RunTelemetry/v1`` importado
   de ``.forge/runs/<id>/telemetry.json`` (IMPORTED, âncora derivada de
   ``receipt.started_at`` — ver forge_bridge.py).
4. Export via ``HTTPExporter`` → ``POST /v1/traces`` (socket real).
5. Leitura de volta: REST (``LensClient``/``GET /v1/traces[/…]``) e MCP
   stdio (``python -m forge_lens mcp-serve`` — initialize/tools/list/
   tools/call reais).
6. Evidência sanitizada em ``docs/integration/evidence/``.

Sem o sibling instalado, os runs caem para os artefatos reais gravados em
``fixtures/captured/`` (replay honesto: provenance=imported ponta a ponta,
``forge_lens.captured_fixture=true``).

Uso:
    .venv/bin/python examples/forge-integration/run_demo.py \
        [--data-dir DIR] [--evidence-dir DIR]
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import forge_bridge as fb

from forge_lens.sdk import LensClient, LensTracer
from forge_lens.sdk.exporter import HTTPExporter

OK_INTENT = "echo hello from the forge-lens integration demo"
FAIL_INTENT = "explode the fixture"


def _log(msg: str) -> None:
    print(f"[forge-demo] {msg}", flush=True)


def run(data_dir: Path, evidence_dir: Path) -> dict[str, Any]:
    """Executa o fluxo inteiro; devolve o resumo (também salvo em evidência)."""
    ws_root = data_dir / "tf-workspace"
    ws_root.mkdir(parents=True, exist_ok=True)

    sanitize_roots = {
        "<data-dir>": str(data_dir.resolve()),
        "<forges>": str(fb.FORGES_ROOT),
        "<repo>": str(fb.REPO_ROOT),
        "<home>": str(Path.home()),
    }

    summary: dict[str, Any] = {
        "integration": "the-forge → forge-lens",
        "mechanism": {
            "root_spans": "observed — subprocess wall-clock via LensTracer",
            "child_spans": "imported — theforge/RunTelemetry/v1 de "
                           ".forge/runs/<id>/telemetry.json, âncora derivada "
                           "de receipt.started_at",
            "transport": "HTTPExporter → POST /v1/traces (socket real)",
            "reads": "REST /v1/traces(+failure) + MCP stdio tools/call",
        },
        "runs": [],
        "evidence": {},
    }

    theforge = fb.find_theforge()
    summary["theforge_argv"] = theforge
    live = theforge is not None

    with fb.lens_server(data_dir) as srv:
        endpoint = srv["endpoint"]
        db = srv["db"]
        _log(f"forge-lens API em {endpoint} (db={db})")
        exporter = HTTPExporter(endpoint, project_id=fb.PROJECT_ID)
        tracer = LensTracer(
            service_name="forge-integration-harness",
            exporter=exporter,
            session_id=fb.SESSION_ID,
            project_id=fb.PROJECT_ID,
            # auto_flush off: o flush explícito abaixo leva os tempos reais
            # já corrigidos (runs capturados anexam receipt.started_at depois
            # do `with` do root — auto-flush serializaria antes disso).
            auto_flush=False,
        )
        client = LensClient(endpoint, project_id=fb.PROJECT_ID)

        runs: list[dict[str, Any]] = []
        if live:
            assert theforge is not None
            env = fb.theforge_env(ws_root)
            fb.init_workspace(theforge, ws_root, env)
            _log("theforge init ok — executando run OK (echo-forge)")
            runs.append(fb.traced_theforge_ask(
                tracer, theforge, ws_root, env,
                intent=OK_INTENT, use=fb.OK_PROVIDER_ID,
                label="ask echo-forge"))
            _log("executando run FAIL (failing-forge → provider_failure)")
            runs.append(fb.traced_theforge_ask(
                tracer, theforge, ws_root, env,
                intent=FAIL_INTENT, use=fb.FAIL_PROVIDER_ID,
                capability=fb.FAIL_CAPABILITY, allow_unverified=True,
                label="ask failing-forge"))
        else:
            _log("the-forge não encontrado — replay dos artefatos REAIS "
                 "capturados em fixtures/captured/")
            for name in ("ok", "fail"):
                runs.append(fb.import_captured_run(
                    tracer, fb.CAPTURED_DIR / name))
        tracer.flush()
        summary["runs"] = runs
        _log(f"runs: {[(r['run_id'], r['run_status']) for r in runs]}")

        # -- leitura REST ---------------------------------------------------
        traces = client.list_traces(limit=50)
        summary["evidence"]["list_traces"] = traces
        ok_id = runs[0]["run_id"]
        fail_id = runs[1]["run_id"]
        # trace_ids internos do Lens (o run_id do theforge é atributo)
        fetched: dict[str, Any] = {}
        for item in traces.get("items", []):
            tid = item["trace_id"]
            fetched[tid] = client.get_trace(tid)
        summary["evidence"]["traces"] = fetched

        fail_trace_id = next(
            (tid for tid, t in fetched.items()
             if any(s.get("status", {}).get("code") == "error"
                    and "failing-forge" in str(s.get("attributes", {}))
                    for s in t.get("spans", []))),
            None,
        ) or next((tid for tid, t in fetched.items()
                   if t.get("status", {}).get("code") == "error"), None)
        failure_report = (
            client.trace_failure(fail_trace_id) if fail_trace_id else None
        )
        summary["evidence"]["failure_classification"] = {
            "trace_id": fail_trace_id, "report": failure_report}
        _log(f"falha classificada: "
             f"{(failure_report or {}).get('category')} "
             f"({(failure_report or {}).get('confidence')})")

        # -- MCP stdio -------------------------------------------------------
        with fb.McpClient(db, cwd=data_dir) as mcp:
            init = mcp.request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "forge-integration-demo",
                               "version": "0.1.0"},
            })
            mcp.notify("notifications/initialized")
            tools = mcp.request("tools/list")
            listed = [t["name"] for t in
                      (tools.get("result") or {}).get("tools", [])]
            mcp_traces = mcp.call_tool("lens.list_traces",
                                       {"project_id": fb.PROJECT_ID})
            mcp_failure = (
                mcp.call_tool("lens.failure",
                              {"trace_id": fail_trace_id,
                               "project_id": fb.PROJECT_ID})
                if fail_trace_id else None
            )
            mcp_stats = mcp.call_tool("lens.stats",
                                      {"project_id": fb.PROJECT_ID})
        summary["evidence"]["mcp"] = {
            "initialize": init.get("result"),
            "tools_listed": listed,
            "list_traces": mcp_traces,
            "failure": mcp_failure,
            "stats": mcp_stats,
        }
        _log(f"MCP tools: {listed}")

    # -- evidência sanitizada -----------------------------------------------
    ev = evidence_dir
    fb.dump_json(ev / "01-run-summary.json", summary["runs"], sanitize_roots)
    fb.dump_json(ev / "02-api-list-traces.json",
                 summary["evidence"]["list_traces"], sanitize_roots)
    fb.dump_json(ev / "03-api-traces.json",
                 summary["evidence"]["traces"], sanitize_roots)
    fb.dump_json(ev / "04-api-failure-classification.json",
                 summary["evidence"]["failure_classification"],
                 sanitize_roots)
    fb.dump_json(ev / "05-mcp-exchange.json",
                 summary["evidence"]["mcp"], sanitize_roots)
    fb.dump_json(ev / "00-demo-metadata.json", {
        "mechanism": summary["mechanism"],
        "theforge_argv": summary["theforge_argv"],
        "live": live,
        "ok_run_id": ok_id,
        "fail_run_id": fail_id,
        "fail_trace_id": fail_trace_id,
    }, sanitize_roots)
    _log(f"evidência em {ev}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="diretório de trabalho (default: tempdir efêmero)")
    parser.add_argument(
        "--evidence-dir", type=Path,
        default=fb.REPO_ROOT / "docs" / "integration" / "evidence")
    parser.add_argument(
        "--keep", action="store_true",
        help="não apagar o tempdir ao final (só faz sentido sem --data-dir)")
    args = parser.parse_args()

    if args.data_dir is not None:
        data_dir = args.data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        summary = run(data_dir, args.evidence_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="forge-lens-demo-") as td:
            summary = run(Path(td), args.evidence_dir)

    ok = all(r.get("imported_spans", 0) > 0 for r in summary["runs"])
    failure = summary["evidence"]["failure_classification"]["report"] or {}
    _log(f"resultado: runs={len(summary['runs'])} "
         f"failure={failure.get('category')}")
    return 0 if ok and failure.get("failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
