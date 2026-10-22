#!/usr/bin/env python3
"""Homologação operacional Forge Lens — cenários A-J.

Executa cenários ponta-a-ponta contra a superfície real instalada
(binário ``forge-lens`` do ``.venv``, API via subprocess ``serve`` e MCP via
stdio), grava evidências em ``homolog/evidence/<cenario>/`` e emite
``homolog/results.json`` + regenera ``docs/homologation.md`` (registro).

Uso:
    .venv/bin/python homolog/run_scenarios.py [--only A,C] [--keep-work]

Cada cenário roda em diretório isolado sob ``homolog/.work/<id>/`` com HOME
própria. As sementes gravam traços reais via ``SQLiteTraceStore`` com os
contratos de domínio — nenhum mock: os spans usam as mesmas classes do
pipeline de produção.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
HOMOLOG = ROOT / "homolog"
WORK = HOMOLOG / ".work"
EVIDENCE = HOMOLOG / "evidence"
RESULTS_JSON = HOMOLOG / "results.json"
REGISTRY_MD = ROOT / "docs" / "homologation.md"

sys.path.insert(0, str(ROOT / "src"))

from forge_lens.contracts.v1.trace import Span, SpanKind, Status, StatusCode, Trace, TraceSource  # noqa: E402
from forge_lens.storage.sqlite import SQLiteTraceStore  # noqa: E402

if os.name == "nt":
    _BIN = ROOT / ".venv" / "Scripts" / "forge-lens.exe"
else:
    _BIN = ROOT / ".venv" / "bin" / "forge-lens"

_CLI_TIMEOUT_S = 60
_HTTP_TIMEOUT_S = 10
_MCP_TIMEOUT_S = 15
_SERVE_READY_S = 30

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_SOURCE = TraceSource(
    system="homolog-runner", sdk_name="homolog", sdk_version="0.0.0",
    host="homolog-host", runtime="python-test", language="python")


# ----------------------------------------------------------------- ctx --


@dataclass
class Step:
    name: str
    ok: bool
    detail: str = ""
    evidence: str | None = None


@dataclass
class ScenarioResult:
    sid: str
    title: str = ""
    objective: str = ""
    expected: str = ""
    steps: list[Step] = field(default_factory=list)
    status: str = "PASS"
    notes: list[str] = field(default_factory=list)


class Ctx:
    """Contexto de execução de um cenário: diretório isolado + evidências."""

    def __init__(self, sid: str, keep_work: bool = False) -> None:
        self.sid = sid
        self.dir = WORK / sid
        self.ev_dir = EVIDENCE / sid
        self.keep_work = keep_work
        self.result = ScenarioResult(sid=sid)
        self._procs: list[subprocess.Popen[str]] = []
        self.env: dict[str, str] = {}

    def __enter__(self) -> Ctx:
        shutil.rmtree(self.dir, ignore_errors=True)
        shutil.rmtree(self.ev_dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.ev_dir.mkdir(parents=True)
        self.env = dict(os.environ)
        self.env["HOME"] = str(self.dir)
        self.env.pop("FORGE_LENS_DB", None)
        return self

    def __exit__(self, *_: Any) -> None:
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        if not self.keep_work:
            shutil.rmtree(self.dir, ignore_errors=True)

    def save_evidence(self, name: str, content: str) -> str:
        path = self.ev_dir / name
        path.write_text(content, encoding="utf-8")
        return str(path.relative_to(ROOT))

    def step(self, name: str, ok: bool, detail: str = "",
             evidence: str | None = None) -> None:
        self.result.steps.append(
            Step(name=name, ok=ok, detail=detail, evidence=evidence))

    def cli(self, *args: str,
            evidence_name: str | None = None) -> subprocess.CompletedProcess[str]:
        """Executa o binário forge-lens real (superfície instalada)."""
        cp = subprocess.run(
            [str(_BIN), *args],
            capture_output=True, text=True, timeout=_CLI_TIMEOUT_S,
            env=self.env, cwd=self.dir,
        )
        if evidence_name:
            self.save_evidence(
                evidence_name,
                f"$ forge-lens {' '.join(args)}\nrc={cp.returncode}\n"
                f"--- stdout ---\n{cp.stdout}\n--- stderr ---\n{cp.stderr}")
        return cp

    def http(self, method: str, base: str, path: str,
             body: Any = None) -> tuple[int, str]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(
            base + path, data=data, method=method,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def serve(self, db: Path) -> str:
        """Sobe `forge-lens serve` real e devolve a base URL quando pronto."""
        port = _free_port()
        base = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen(
            [str(_BIN), "serve", "--db", str(db),
             "--host", "127.0.0.1", "--port", str(port)],
            env=self.env, cwd=self.dir,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        self._procs.append(proc)
        deadline = time.monotonic() + _SERVE_READY_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"serve exited early rc={proc.returncode}")
            try:
                status, _ = self.http("GET", base, "/v1/health")
                if status == 200:
                    return base
            except OSError:
                pass
            time.sleep(0.25)
        raise RuntimeError("serve did not become ready")

    def mcp(self, db: Path) -> McpSession:
        proc = subprocess.Popen(
            [str(_BIN), "mcp-serve", "--db", str(db)],
            env=self.env, cwd=self.dir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True)
        self._procs.append(proc)
        return McpSession(proc)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class McpSession:
    """Cliente JSON-RPC mínimo sobre stdio newline-delimited."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self.proc = proc
        self._id = 0
        self.frames: list[dict[str, Any]] = []

    def call(self, method: str,
             params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._id += 1
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id,
                               "method": method}
        if params is not None:
            msg["params"] = params
        assert self.proc.stdin
        assert self.proc.stdout
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        resp = json.loads(_readline_timeout(self.proc.stdout, _MCP_TIMEOUT_S))
        self.frames.append({"request": msg, "response": resp})
        return resp

    def notify(self, method: str) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()


def _readline_timeout(stream: Any, timeout_s: float) -> str:
    holder: list[str] = []

    def _r() -> None:
        holder.append(stream.readline())

    t = threading.Thread(target=_r, daemon=True)
    t.start()
    t.join(timeout_s)
    if not holder or holder[0] == "":
        raise TimeoutError("MCP: sem resposta no stdio")
    return holder[0]


# --------------------------------------------------------------- seeds --


def _span(tid: str, sid: str, name: str, kind: SpanKind, parent: str | None,
          t0: datetime, dur_ms: int, status: StatusCode = StatusCode.OK,
          message: str | None = None,
          attrs: dict[str, Any] | None = None) -> Span:
    return Span(
        trace_id=tid, span_id=sid, parent_span_id=parent, name=name,
        kind=kind, start_time=t0, end_time=t0 + timedelta(milliseconds=dur_ms),
        attributes=attrs or {}, status=Status(code=status, message=message))


def _store(db: Path) -> SQLiteTraceStore:
    return SQLiteTraceStore(db)


def seed_basic_trace(db: Path, tid: str, *, fail: bool = False,
                     t0: datetime = _T0, project_id: str = "default",
                     session_id: str | None = "sess-homolog",
                     extra_root_attrs: dict[str, Any] | None = None) -> None:
    """Traço real: agent → llm + tool (com falha opcional)."""
    tool_status = StatusCode.ERROR if fail else StatusCode.OK
    tool_attrs: dict[str, Any] = {
        "tool.name": "search_web",
        "gen_ai.tool.name": "search_web",
        "gen_ai.operation.name": "execute_tool",
    }
    if fail:
        tool_attrs["exception.type"] = "ToolTimeoutError"
        tool_attrs["exception.message"] = "timed out after 30s"
    root_attrs = {"gen_ai.agent.name": "homolog-agent",
                  "gen_ai.system": "forge-lens-homolog",
                  **(extra_root_attrs or {})}
    agent = _span(tid, "1" * 16, "agent.run", SpanKind.AGENT, None, t0, 900,
                  attrs=root_attrs)
    llm = _span(tid, "2" * 16, "llm gpt-test", SpanKind.LLM, "1" * 16,
                t0 + timedelta(milliseconds=50), 350,
                attrs={"llm.model_name": "gpt-test",
                       "gen_ai.request.model": "gpt-test",
                       "forge_lens.token_usage": {
                           "provider": "openai", "model": "gpt-test",
                           "input_tokens": 100, "output_tokens": 42,
                           "total_tokens": 142,
                           "measurement_source": "provider"}})
    tool = _span(tid, "3" * 16, "tool search_web", SpanKind.TOOL, "1" * 16,
                 t0 + timedelta(milliseconds=420), 480, tool_status,
                 message="timeout" if fail else None, attrs=tool_attrs)
    trace = Trace(
        trace_id=tid, root_span_id=agent.span_id,
        started_at=t0, ended_at=t0 + timedelta(milliseconds=900),
        status=Status(code=StatusCode.ERROR if fail else StatusCode.OK),
        source=_SOURCE, spans=[agent, llm, tool],
        session_id=session_id, project_id=project_id)
    store = _store(db)
    try:
        store.upsert_trace(trace)
    finally:
        store.close()


def seed_scaled_trace(db: Path, n_spans: int, tid: str,
                      t0: datetime = _T0) -> None:
    """Traço com N spans — árvore agent→tool determinística."""
    spans: list[Span] = [
        _span(tid, "1" * 16, "agent.run", SpanKind.AGENT, None, t0,
              n_spans * 10,
              attrs={"gen_ai.agent.name": "scale-agent",
                     "gen_ai.system": "forge-lens-homolog"})
    ]
    for i in range(2, n_spans + 1):
        sid = f"{i:016x}"
        parent = f"{i - 1:016x}" if i <= 200 else "1" * 16
        spans.append(_span(
            tid, sid, f"tool op-{i}", SpanKind.TOOL, parent,
            t0 + timedelta(milliseconds=i * 8), 6,
            attrs={"gen_ai.tool.name": f"op-{i % 50}",
                   "gen_ai.operation.name": "execute_tool"}))
    trace = Trace(
        trace_id=tid, root_span_id=spans[0].span_id,
        started_at=t0, ended_at=t0 + timedelta(milliseconds=n_spans * 10),
        status=Status(), source=_SOURCE, spans=spans, project_id="default")
    store = _store(db)
    try:
        store.upsert_trace(trace)
    finally:
        store.close()


# ------------------------------------------------------------ scenarios --


def scenario_a(ctx: Ctx) -> None:
    ctx.result.title = "A — Deploy local: init → doctor → serve → inspect"
    ctx.result.objective = (
        "Subir a stack local do zero e inspecionar saúde/capacidades.")
    ctx.result.expected = (
        "init cria .forge-lens; doctor saudável; serve responde /v1/health, "
        "/v1/ops/summary e /v1/capabilities.")

    cp = ctx.cli("init", ".", evidence_name="01-init.txt")
    ok = cp.returncode == 0 and (ctx.dir / ".forge-lens").exists()
    ctx.step("init", ok, f"rc={cp.returncode}", "01-init.txt")

    db = ctx.dir / "data.db"
    seed_basic_trace(db, "a" * 32)
    ctx.step("seed", db.exists(), "trace de exemplo persistido")

    cp = ctx.cli("doctor", ".", "--db", str(db), evidence_name="02-doctor.txt")
    ctx.step("doctor", cp.returncode == 0, f"rc={cp.returncode}",
             "02-doctor.txt")

    cp = ctx.cli("capabilities", evidence_name="03-capabilities.txt")
    ctx.step("capabilities (CLI)", cp.returncode == 0, f"rc={cp.returncode}",
             "03-capabilities.txt")

    base = ctx.serve(db)
    ctx.step("serve", True, base)

    status, body = ctx.http("GET", base, "/v1/health")
    ev = ctx.save_evidence("04-health.json", body)
    ctx.step("GET /v1/health", status == 200, f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, "/v1/ops/summary")
    ev = ctx.save_evidence("05-ops-summary.json", body)
    ctx.step("GET /v1/ops/summary", status == 200, f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, "/v1/capabilities")
    ev = ctx.save_evidence("06-capabilities.json", body)
    ctx.step("GET /v1/capabilities", status == 200, f"HTTP {status}", ev)

    ctx.result.notes.append(
        "Studio (apps/studio) é servido separadamente (ng serve/build) — "
        "homologado na suíte E2E Playwright, ver docs/studio-e2e.md.")


def scenario_b(ctx: Ctx) -> None:
    ctx.result.title = "B — Ingest real → traces → detail → cost → graph → MCP"
    ctx.result.objective = (
        "Traços persistidos consultáveis via REST e via MCP stdio.")
    ctx.result.expected = (
        "Traço visível em /v1/traces, detalhe completo, /cost deriva custo, "
        "/graph/traces monta a árvore, MCP lens.list_traces + lens.get_trace respondem.")

    db = ctx.dir / "data.db"
    tid = "b" * 32
    seed_basic_trace(db, tid)
    ctx.step("seed trace", True, tid)

    base = ctx.serve(db)
    ctx.step("serve", True, base)

    status, body = ctx.http("GET", base, "/v1/traces")
    ev = ctx.save_evidence("01-traces.json", body)
    ctx.step("GET /v1/traces", status == 200 and tid in body,
             f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, f"/v1/traces/{tid}")
    ev = ctx.save_evidence("02-trace-detail.json", body)
    ctx.step("GET /v1/traces/{id}", status == 200 and "spans" in body,
             f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, f"/v1/traces/{tid}/cost")
    ev = ctx.save_evidence("03-cost.json", body)
    ctx.step("GET /v1/traces/{id}/cost", status == 200, f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, f"/v1/graph/traces/{tid}")
    ev = ctx.save_evidence("04-graph.json", body)
    ctx.step("GET /v1/graph/traces/{id}", status == 200, f"HTTP {status}", ev)

    mcp = ctx.mcp(db)
    resp = mcp.call("initialize", {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "homolog", "version": "0"}})
    ctx.step("MCP initialize", "result" in resp,
             json.dumps(resp.get("result", {}))[:160])

    mcp.notify("notifications/initialized")
    resp = mcp.call("tools/list")
    tools = [t["name"] for t in resp.get("result", {}).get("tools", [])]
    ctx.step("MCP tools/list", len(tools) > 0, f"{len(tools)} tools")

    resp = mcp.call("tools/call", {
        "name": "lens.list_traces", "arguments": {"limit": 5}})
    is_err = bool(resp.get("result", {}).get("isError"))
    ctx.step("MCP lens.list_traces", "result" in resp and not is_err)

    resp = mcp.call("tools/call", {
        "name": "lens.get_trace", "arguments": {"trace_id": tid}})
    is_err = bool(resp.get("result", {}).get("isError"))
    ctx.step("MCP lens.get_trace", "result" in resp and not is_err, tid)

    ev = ctx.save_evidence(
        "05-mcp-session.json",
        json.dumps(mcp.frames, indent=2, ensure_ascii=False))
    ctx.result.notes.append(f"Frames MCP completos em {ev}")


def scenario_c(ctx: Ctx) -> None:
    ctx.result.title = "C — Investigação: failure → replay → incident bundle"
    ctx.result.objective = (
        "Investigar um traço com falha e exportar o bundle de evidências.")
    ctx.result.expected = (
        "failure classifica a falha; replay monta a timeline; incident gera "
        "bundle sem vazar segredos.")

    db = ctx.dir / "data.db"
    tid = "c" * 32
    secret = "sk-ant-homolog-SECRET-DO-NOT-LEAK-12345"
    seed_basic_trace(db, tid, fail=True,
                     extra_root_attrs={
                         "llm.prompt": f"user typed key {secret} check"})
    ctx.step("seed failing trace + embedded secret", True, tid)

    base = ctx.serve(db)
    ctx.step("serve", True, base)

    status, body = ctx.http("GET", base, f"/v1/traces/{tid}/failure")
    ev = ctx.save_evidence("01-failure.json", body)
    ctx.step("GET /v1/traces/{id}/failure", status == 200, f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, f"/v1/traces/{tid}/replay")
    ev = ctx.save_evidence("02-replay.json", body)
    ctx.step("GET /v1/traces/{id}/replay", status == 200, f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, f"/v1/traces/{tid}/incident")
    ev = ctx.save_evidence("03-incident.json", body)
    leaked = secret in body
    ctx.step("GET /v1/traces/{id}/incident",
             status == 200 and not leaked,
             f"HTTP {status}; segredo {'VAZADO!' if leaked else 'não vazado'}",
             ev)

    cp = ctx.cli("failure", tid, "--db", str(db),
                 evidence_name="04-cli-failure.txt")
    ctx.step("CLI failure", cp.returncode == 0, f"rc={cp.returncode}",
             "04-cli-failure.txt")

    cp = ctx.cli("replay", tid, "--db", str(db),
                 evidence_name="05-cli-replay.txt")
    ctx.step("CLI replay", cp.returncode == 0, f"rc={cp.returncode}",
             "05-cli-replay.txt")


def scenario_d(ctx: Ctx) -> None:
    ctx.result.title = "D — Graph em escala (≥1000 spans)"
    ctx.result.objective = (
        "Construir e consultar o grafo de um traço com ≥1000 spans.")
    ctx.result.expected = (
        "/v1/graph/traces/{id} responde com todos os nós/arestas; navegação "
        "no Studio documentada em docs/graph-scale.md.")

    db = ctx.dir / "data.db"
    tid = "d" * 32
    n = 1200
    seed_scaled_trace(db, n, tid)
    ctx.step("seed 1200-span trace", True, f"{n} spans")

    base = ctx.serve(db)
    ctx.step("serve", True, base)

    t0 = time.monotonic()
    status, body = ctx.http("GET", base, f"/v1/graph/traces/{tid}")
    ms = (time.monotonic() - t0) * 1000
    ev = ctx.save_evidence("01-graph-1200.json", body)
    ctx.step("GET /v1/graph/traces/{id} (1200 spans)", status == 200,
             f"HTTP {status}, {ms:.0f} ms, {len(body)} bytes", ev)

    status, body = ctx.http("GET", base, f"/v1/graph/traces/{tid}/analysis")
    ev = ctx.save_evidence("02-graph-analysis.json", body)
    ctx.step("GET .../analysis", status == 200, f"HTTP {status}", ev)

    status, body = ctx.http("GET", base, f"/v1/graph/traces/{tid}/critical-path")
    ev = ctx.save_evidence("03-critical-path.json", body)
    ctx.step("GET .../critical-path", status == 200, f"HTTP {status}", ev)

    ctx.result.notes.append(
        "Render/navegação em escala (cap, busca, fit) é homologada no Studio — "
        "ver docs/graph-scale.md e os unit tests do apps/studio.")


def scenario_e(ctx: Ctx) -> None:
    ctx.result.title = "E — Eval: baseline vs candidato → compare → gate"
    ctx.result.objective = (
        "Rodar avaliadores em coortes baseline/candidato e comparar com stats.")
    ctx.result.expected = (
        "/v1/eval/run avalia; /v1/eval/compare retorna deltas + testes "
        "estatísticos; gate CI documentado (scripts/eval_gate.py).")

    db = ctx.dir / "data.db"
    tid_ok = "e0" + "0" * 30
    tid_fail = "e1" + "1" * 30
    seed_basic_trace(db, tid_ok, t0=_T0)
    seed_basic_trace(db, tid_fail, fail=True, t0=_T0 + timedelta(hours=1))
    ctx.step("seed baseline ok + candidato regressado", True,
             f"baseline={tid_ok[:4]} cand={tid_fail[:4]}")

    base = ctx.serve(db)
    ctx.step("serve", True, base)

    evaluators = [{"name": "error_free"}, {"name": "tool_success"}]
    status, body = ctx.http("POST", base, "/v1/eval/run", {
        "trace_ids": [tid_ok, tid_fail], "evaluators": evaluators})
    ev = ctx.save_evidence("01-eval-run.json", body)
    ctx.step("POST /v1/eval/run", status == 200, f"HTTP {status}", ev)

    status, body = ctx.http("POST", base, "/v1/eval/compare", {
        "a": {"trace_ids": [tid_ok]}, "b": {"trace_ids": [tid_fail]},
        "evaluators": evaluators})
    ev = ctx.save_evidence("02-eval-compare.json", body)
    ctx.step("POST /v1/eval/compare", status == 200, f"HTTP {status}", ev)

    cp = ctx.cli("eval", "--db", str(db), tid_ok,
                 evidence_name="03-cli-eval.txt")
    ctx.step("CLI eval", cp.returncode == 0, f"rc={cp.returncode}",
             "03-cli-eval.txt")

    ctx.result.notes.append(
        "Gate de CI para adoção por outros projetos: scripts/eval_gate.py + "
        "docs/eval-gate-workflow-example.yml.")


def scenario_f(ctx: Ctx) -> None:
    ctx.result.title = "F — Sanitização de segredos em todas as leituras"
    ctx.result.objective = (
        "Garantir que credenciais em spans nunca saem por nenhuma superfície.")
    ctx.result.expected = (
        "API, CLI, MCP e incident export exibem conteúdo mascarado; "
        "contadores de sanitização são declarados.")

    db = ctx.dir / "data.db"
    tid = "f" * 32
    secrets = {
        "api_key": "sk-ant-homolog-SECRET-67890",
        "auth": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.homolog",
        "url": "https://user:p4ssw0rd@internal.corp/api",
        "pwd": "password=Sup3rSecret!",
    }

    base = ctx.serve(db)
    ctx.step("serve", True, base)

    # semente via a fronteira REAL de ingestão (sanitize acontece aqui —
    # escrever direto na store não exercita o controle de produção)
    span_attrs = {
        "gen_ai.agent.name": "sec-agent",
        "llm.prompt": "cfg: " + "; ".join(secrets.values()),
        "http.headers.Authorization": secrets["auth"],
        "http.url": secrets["url"],
        "env.password": secrets["pwd"],
    }
    span = _span(tid, "1" * 16, "agent.run", SpanKind.AGENT, None, _T0, 500,
                 attrs=span_attrs)
    ingest = {
        "project_id": "default",
        "source": _SOURCE.model_dump(mode="json"),
        "traces": [Trace(
            trace_id=tid, root_span_id=span.span_id, started_at=_T0,
            ended_at=_T0 + timedelta(milliseconds=500), status=Status(),
            source=_SOURCE, spans=[span]).model_dump(mode="json")],
    }
    status, body = ctx.http("POST", base, "/v1/traces", ingest)
    ev = ctx.save_evidence("00-ingest-response.json", body)
    ctx.step("POST /v1/traces (ingest com segredos)", status == 200,
             f"HTTP {status}", ev)

    def _check(name: str, status: int, body: str, evidence_name: str) -> None:
        ev = ctx.save_evidence(evidence_name, body)
        leaked = [k for k, v in secrets.items() if v in body]
        ctx.step(name, status == 200 and not leaked,
                 f"HTTP {status}; vazaram: {leaked or 'nenhum'}", ev)

    s, b = ctx.http("GET", base, f"/v1/traces/{tid}")
    _check("API GET /v1/traces/{id}", s, b, "01-api-trace.json")

    s, b = ctx.http("GET", base, f"/v1/traces/{tid}/incident")
    _check("API incident export", s, b, "02-api-incident.json")

    cp = ctx.cli("traces", "show", tid, "--db", str(db),
                 evidence_name="03-cli-show.txt")
    leaked_cli = [k for k, v in secrets.items() if v in cp.stdout]
    ctx.step("CLI traces show", cp.returncode == 0 and not leaked_cli,
             f"rc={cp.returncode}; vazaram: {leaked_cli or 'nenhum'}",
             "03-cli-show.txt")

    mcp = ctx.mcp(db)
    mcp.call("initialize", {"protocolVersion": "2024-11-05",
                            "capabilities": {},
                            "clientInfo": {"name": "h", "version": "0"}})
    resp = mcp.call("tools/call", {"name": "lens.get_trace",
                                   "arguments": {"trace_id": tid}})
    ev = ctx.save_evidence("04-mcp-trace.json",
                           json.dumps(resp, indent=2, ensure_ascii=False))
    leaked_mcp = [k for k, v in secrets.items() if v in json.dumps(resp)]
    ctx.step("MCP lens.get_trace", not leaked_mcp,
             f"vazaram: {leaked_mcp or 'nenhum'}", ev)


def scenario_g(ctx: Ctx) -> None:
    ctx.result.title = "G — Export/pack de evidências"
    ctx.result.objective = (
        "Exportar o pacote de evidências (incident bundle) e validar conteúdo.")
    ctx.result.expected = (
        "Bundle contém trace + classificação + timeline + evidências; JSON "
        "parseável; contadores de export_sanitize declarados.")

    db = ctx.dir / "data.db"
    tid = "a7" + "7" * 30
    seed_basic_trace(db, tid, fail=True)
    ctx.step("seed failing trace", True, tid)

    base = ctx.serve(db)
    ctx.step("serve", True, base)

    status, body = ctx.http("GET", base, f"/v1/traces/{tid}/incident")
    ev = ctx.save_evidence("01-incident-bundle.json", body)
    parsed = json.loads(body) if status == 200 else {}
    ctx.step("GET /v1/traces/{id}/incident",
             status == 200 and "trace_id" in parsed,
             f"HTTP {status}; chaves={sorted(parsed.keys())}", ev)

    cp = ctx.cli("incident", tid, "--db", str(db),
                 evidence_name="02-cli-incident.txt")
    ctx.step("CLI incident", cp.returncode == 0, f"rc={cp.returncode}",
             "02-cli-incident.txt")


def scenario_h(ctx: Ctx) -> None:
    ctx.result.title = "H — Retenção: prune + archive"
    ctx.result.objective = (
        "Aplicar retenção: arquivar e apagar traços antigos com auditoria.")
    ctx.result.expected = (
        "archive gera NDJSON; prune remove traços antigos; operação registrada "
        "em audit; traços novos permanecem.")

    db = ctx.dir / "data.db"
    # retenção é relativa ao wall-clock — sementes precisam de datas reais
    now = datetime.now(tz=UTC)
    seed_basic_trace(db, "b0" + "0" * 30, t0=now - timedelta(days=120))
    seed_basic_trace(db, "b1" + "1" * 30, t0=now)
    ctx.step("seed 1 traço antigo (120d) + 1 recente", True)

    ndjson = ctx.dir / "archive.ndjson"
    cp = ctx.cli("archive", "--db", str(db),
                 "--before", (now - timedelta(days=30)).isoformat(),
                 "--out", str(ndjson),
                 evidence_name="01-archive.txt")
    ctx.step("CLI archive", cp.returncode == 0 and ndjson.exists(),
             f"rc={cp.returncode}; arquivo={'ok' if ndjson.exists() else 'ausente'}",
             "01-archive.txt")

    cp = ctx.cli("prune", "--db", str(db), "--days", "30", "-y",
                 evidence_name="02-prune.txt")
    ctx.step("CLI prune -y", cp.returncode == 0, f"rc={cp.returncode}",
             "02-prune.txt")

    store = _store(db)
    try:
        remaining = store.list_traces(limit=50, offset=0)
    finally:
        store.close()
    ctx.step("verificação pós-prune", len(remaining) == 1,
             f"{len(remaining)} traço(s) restante(s)")

    cp = ctx.cli("traces", "list", "--db", str(db),
                 evidence_name="03-traces-list.txt")
    ctx.step("traces list pós-prune", cp.returncode == 0,
             f"rc={cp.returncode}", "03-traces-list.txt")


def scenario_i(ctx: Ctx) -> None:
    ctx.result.title = "I — Backup e restauração"
    ctx.result.objective = (
        "Copiar a store (WAL checkpoint + cópia) e reabrir com conteúdo íntegro.")
    ctx.result.expected = (
        "DB copiado abre readonly com os mesmos traços; archive NDJSON cobre "
        "o restore lógico.")

    db = ctx.dir / "data.db"
    seed_basic_trace(db, "c0" + "0" * 30)
    seed_basic_trace(db, "c1" + "1" * 30, t0=_T0 + timedelta(hours=1))
    ctx.step("seed 2 traços", True)

    import sqlite3 as _sq
    conn = _sq.connect(db)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    backup = ctx.dir / "backup.db"
    shutil.copy2(db, backup)
    ctx.step("WAL checkpoint + cópia física", backup.exists())

    store = SQLiteTraceStore(backup, readonly=True)
    try:
        got = store.list_traces(limit=50, offset=0)
    finally:
        store.close()
    ctx.step("abrir backup readonly", len(got) == 2,
             f"{len(got)} traço(s) no backup")

    ndjson = ctx.dir / "backup.ndjson"
    cp = ctx.cli("archive", "--db", str(db),
                 "--before", "2260-01-01T00:00:00+00:00",
                 "--out", str(ndjson), evidence_name="01-archive-all.txt")
    lines = ndjson.read_text().strip().splitlines() if ndjson.exists() else []
    ctx.step("archive completo (backup lógico)",
             cp.returncode == 0 and len(lines) >= 1,
             f"{len(lines)} linha(s) NDJSON", "01-archive-all.txt")


def scenario_j(ctx: Ctx) -> None:
    ctx.result.title = "J — Operação 100% offline"
    ctx.result.objective = (
        "Provar que toda a superfície local opera sem rede (proxies mortos).")
    ctx.result.expected = (
        "Com HTTP(S)_PROXY apontando para endereço morto e HOME isolada, "
        "init/serve/API/MCP funcionam — nenhuma chamada externa é tentada.")

    ctx.env["HTTP_PROXY"] = "http://127.0.0.1:1"
    ctx.env["HTTPS_PROXY"] = "http://127.0.0.1:1"
    ctx.env["ALL_PROXY"] = "http://127.0.0.1:1"
    ctx.env["NO_PROXY"] = ""

    cp = ctx.cli("init", ".", evidence_name="01-init-offline.txt")
    ctx.step("init offline", cp.returncode == 0, f"rc={cp.returncode}",
             "01-init-offline.txt")

    db = ctx.dir / "data.db"
    seed_basic_trace(db, "a1" + "1" * 30)
    base = ctx.serve(db)
    ctx.step("serve offline", True, base)

    status, _ = ctx.http("GET", base, "/v1/traces")
    ctx.step("GET /v1/traces offline", status == 200, f"HTTP {status}")

    mcp = ctx.mcp(db)
    resp = mcp.call("initialize", {"protocolVersion": "2024-11-05",
                                   "capabilities": {},
                                   "clientInfo": {"name": "h", "version": "0"}})
    ctx.step("MCP offline", "result" in resp)
    ctx.save_evidence("02-offline-ok.json",
                      json.dumps({"traces_status": status, "mcp": resp},
                                 indent=2, ensure_ascii=False))


SCENARIOS: list[tuple[str, Any]] = [
    ("A", scenario_a), ("B", scenario_b), ("C", scenario_c),
    ("D", scenario_d), ("E", scenario_e), ("F", scenario_f),
    ("G", scenario_g), ("H", scenario_h), ("I", scenario_i),
    ("J", scenario_j),
]


# -------------------------------------------------------------- driver --


def run_one(sid: str, fn: Any, keep_work: bool) -> ScenarioResult:
    with Ctx(sid, keep_work) as ctx:
        try:
            fn(ctx)
        except Exception as exc:
            ctx.result.steps.append(
                Step(name="(exceção)", ok=False, detail=repr(exc)))
        if any(not s.ok for s in ctx.result.steps):
            ctx.result.status = "FAIL"
        return ctx.result


def write_registry(results: list[ScenarioResult]) -> None:
    lines = [
        "# Homologação operacional — cenários A-J",
        "",
        "Execução ponta-a-ponta contra a superfície instalada (binário "
        "`forge-lens`",
        "do `.venv`, API `forge-lens serve` em subprocess, MCP por stdio). "
        "Regenerado por `homolog/run_scenarios.py`.",
        "",
        "| Cenário | Objetivo | Status | Passos | Evidências |",
        "|---|---|---|---|---|",
    ]
    def _ev(r: ScenarioResult, s: Step) -> str | None:
        if not s.evidence:
            return None
        if "/" in s.evidence:
            return s.evidence
        return f"homolog/evidence/{r.sid}/{s.evidence}"

    for r in results:
        evs = "; ".join(
            f"`{e}`" for e in (_ev(r, s) for s in r.steps) if e) or "—"
        ok_steps = sum(1 for s in r.steps if s.ok)
        lines.append(
            f"| {r.title} | {r.objective} | **{r.status}** | "
            f"{ok_steps}/{len(r.steps)} | {evs} |")
    lines += ["", "## Detalhes por cenário", ""]
    for r in results:
        lines += [f"### {r.title}", "",
                  f"- **Objetivo:** {r.objective}",
                  f"- **Esperado:** {r.expected}",
                  f"- **Status:** {r.status}", ""]
        for s in r.steps:
            mark = "✅" if s.ok else "❌"
            evp = _ev(r, s)
            ev = f" — evidência: `{evp}`" if evp else ""
            detail = f" — {s.detail}" if s.detail else ""
            lines.append(f"- {mark} `{s.name}`{detail}{ev}")
        if r.notes:
            lines.append("")
            for n in r.notes:
                lines.append(f"  _Nota:_ {n}")
        lines.append("")
    REGISTRY_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="rodar só estes cenários, ex: A,C")
    ap.add_argument("--keep-work", action="store_true")
    args = ap.parse_args()

    if not _BIN.exists():
        print(f"binário forge-lens não encontrado: {_BIN}", file=sys.stderr)
        return 2

    only = set(args.only.split(",")) if args.only else None
    results: list[ScenarioResult] = []
    for sid, fn in SCENARIOS:
        if only and sid not in only:
            continue
        print(f"=== cenário {sid} ===", flush=True)
        r = run_one(sid, fn, args.keep_work)
        for s in r.steps:
            print(f"  [{'OK' if s.ok else 'FAIL'}] {s.name} {s.detail}")
        print(f"  → {r.status}")
        results.append(r)

    RESULTS_JSON.write_text(json.dumps(
        {"generated_at": datetime.now(tz=UTC).isoformat(),
         "scenarios": [
             {"id": r.sid, "title": r.title, "status": r.status,
              "objective": r.objective, "expected": r.expected,
              "notes": r.notes,
              "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail,
                         "evidence": s.evidence} for s in r.steps]}
             for r in results]},
        indent=2, ensure_ascii=False), encoding="utf-8")
    write_registry(results)

    failed = [r.sid for r in results if r.status != "PASS"]
    print(f"\nResultados: {len(results) - len(failed)} OK, "
          f"{len(failed)} FAIL {failed or ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
