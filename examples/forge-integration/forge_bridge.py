# ruff: noqa: S603 — subprocess de siblings é o ponto deste harness
# (mesmo precedente de homolog/** no per-file-ignores do projeto).
"""Ponte the-forge → Forge Lens — importação de telemetria REAL de execução.

O que é observado (provenance="observed"):
  - O span-raiz de cada run: `theforge ask ...` executado de verdade via
    subprocess; wall-clock medido pelo harness (LensTracer), exit code real.

O que é importado (provenance="imported"):
  - Os spans internos do run, lidos de ``.forge/runs/<id>/telemetry.json``
    (contrato ``theforge/RunTelemetry/v1`` — medidos pelo recorder monotônico
    do próprio theforge, com ``start_ms``/``duration_ms`` reais).
  - O status/erro do run, lidos de ``receipt.json``/``result.json``.

Derivação documentada: os spans do theforge usam relógio monotônico relativo
(offsets ``start_ms`` a partir da criação do recorder). Para ancorar em tempo
de parede usamos ``receipt.started_at`` (fallback: ``task.created_at`` e, por
último, ``telemetry.created_at`` menos o fim do último span). Nada disso é
inventado: durações e offsets são os valores medidos; apenas a origem do eixo
de tempo é derivada — marcada em ``theforge.anchored_at``.

Sem sibling presente, ``import_captured_run`` re-importa artefatos gravados de
uma execução real anterior (fixtures/captured/) — dados REAIS capturados,
rotulados ``imported`` + ``forge_lens.captured_fixture``.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forge_lens.contracts.provenance import Provenance
from forge_lens.contracts.v1.trace import SpanKind, Status, StatusCode

MODULE_DIR = Path(__file__).resolve().parent
REPO_ROOT = MODULE_DIR.parents[1]
FORGES_ROOT = REPO_ROOT.parent
THEFORGE_REPO = FORGES_ROOT / "the-forge"
FIXTURE_PROVIDER = MODULE_DIR / "fixtures" / "failing_provider.py"
CAPTURED_DIR = MODULE_DIR / "fixtures" / "captured"

PROJECT_ID = "forge-integration"
SESSION_ID = "forge-integration-demo"

FAIL_PROVIDER_ID = "failing-forge"
FAIL_CAPABILITY = "demo.failcrash"
OK_PROVIDER_ID = "echo-forge"

_RUN_ARTIFACTS = ("task.json", "receipt.json", "telemetry.json",
                  "routing.json", "result.json")


# --------------------------------------------------------------------------
# resolução do sibling the-forge
# --------------------------------------------------------------------------

def find_theforge() -> list[str] | None:
    """Argv prefix para invocar o CLI real do the-forge, ou None.

    Ordem: ``FORGE_LENS_THEFORGE`` (caminho explícito ou argv `python -m`),
    ``<forjas>/the-forge/.venv`` (script ou ``python -m theforge``), e por
    fim ``theforge`` no PATH.
    """
    explicit = os.environ.get("FORGE_LENS_THEFORGE")
    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file() and os.access(p, os.X_OK):
            return [str(p)]
        # aceita "python -m theforge" estilo argv único já resolvido
        return explicit.split()

    venv = THEFORGE_REPO / ".venv"
    script = venv / ("Scripts/theforge.exe" if os.name == "nt" else "bin/theforge")
    if script.is_file():
        return [str(script)]
    py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if py.is_file():
        probe = subprocess.run(
            [str(py), "-c", "import theforge"],
            capture_output=True, timeout=30,
        )
        if probe.returncode == 0:
            return [str(py), "-m", "theforge"]

    on_path = shutil.which("theforge")
    if on_path:
        return [on_path]
    return None


def theforge_env(ws: Path) -> dict[str, str]:
    """Env isolada para o subprocess: sem providers de usuário nem cache
    compartilhado — só builtin + o que o workspace declara."""
    env = dict(os.environ)
    home = ws / ".theforge-home"
    env["THEFORGE_CONFIG_DIR"] = str(home / "config")
    env["THEFORGE_CACHE_DIR"] = str(home / "cache")
    return env


def init_workspace(theforge: list[str], ws: Path, env: dict[str, str]) -> None:
    """``theforge init`` real + provider fixture de falha no providers.toml."""
    subprocess.run(
        [*theforge, "init", "--root", str(ws)],
        check=True, capture_output=True, text=True, env=env, timeout=120,
    )
    providers = ws / ".forge" / "config" / "providers.toml"
    with providers.open("a", encoding="utf-8") as fh:
        fh.write(
            '\n[[providers]]\n'
            f'id = "{FAIL_PROVIDER_ID}"\n'
            f'argv = ["{{python}}", "{FIXTURE_PROVIDER}"]\n'
        )


def run_ask(
    theforge: list[str],
    ws: Path,
    env: dict[str, str],
    intent: str,
    *,
    use: str,
    capability: str | None = None,
    allow_unverified: bool = False,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Executa ``theforge ask`` de verdade. Retorna o envelope --json real."""
    argv = [*theforge, "ask", intent, "--root", str(ws), "--json",
            "--use", use]
    if capability:
        argv += ["--capability", capability]
    if allow_unverified:
        argv += ["--allow-unverified"]
    proc = subprocess.run(
        argv, capture_output=True, text=True, env=env, timeout=timeout_s,
    )
    out: dict[str, Any] = {
        "argv": argv,
        "exit_code": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-1000:],
    }
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        out["parse_error"] = "stdout is not JSON"
        out["stdout_tail"] = (proc.stdout or "")[-1000:]
        return out
    out["run_id"] = envelope.get("run_id")
    out["run_status"] = envelope.get("status")
    out["envelope"] = envelope
    return out


# --------------------------------------------------------------------------
# importação theforge/RunTelemetry/v1 → spans Forge Lens
# --------------------------------------------------------------------------

def _iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _anchor(receipt: dict[str, Any], task: dict[str, Any],
            telemetry: dict[str, Any]) -> tuple[datetime | None, str]:
    """Base de tempo de parede para os offsets monotônicos do theforge."""
    for source, doc, key in (
        ("receipt.started_at", receipt, "started_at"),
        ("task.created_at", task, "created_at"),
    ):
        ts = _iso(doc.get(key))
        if ts is not None:
            return ts, source
    created = _iso(telemetry.get("created_at"))
    if created is not None:
        ends = [float(s.get("start_ms", 0)) + float(s.get("duration_ms", 0))
                for s in telemetry.get("spans", [])]
        if ends:
            return created - timedelta(milliseconds=max(ends)), \
                "telemetry.created_at-last_span_end"
    return None, "unanchored"


def _bounded(value: Any, limit: int = 500) -> str:
    return str(value)[:limit]


def import_run_spans(
    tracer: Any,
    run_dir: Path,
) -> dict[str, Any]:
    """Importa os spans nativos do run como filhos do span corrente.

    Deve ser chamada dentro de um ``tracer.start_span(...)`` (o pai vem do
    context stack). Cada span importado carrega provenance=IMPORTED e os
    campos originais ``theforge.*`` — a posição no tempo é derivada da âncora
    documentada, nunca medida pelo harness.
    """
    telemetry = json.loads((run_dir / "telemetry.json").read_text("utf-8"))
    receipt = _read_json(run_dir / "receipt.json")
    task = _read_json(run_dir / "task.json")
    result = _read_json(run_dir / "result.json")
    routing = _read_json(run_dir / "routing.json")

    base, anchor_src = _anchor(receipt, task, telemetry)
    base = base or datetime.now(tz=UTC)

    spans = telemetry.get("spans") or []
    id_map: dict[str, str] = {}  # theforge span id -> lens span id
    imported = 0
    for raw in spans:
        name = str(raw.get("name", "span"))
        tf_id = str(raw.get("id", f"s{imported}"))
        is_provider = name.startswith("provider:")
        attrs: dict[str, Any] = {
            "theforge.run_id": telemetry.get("run_id"),
            "theforge.span_id": tf_id,
            "theforge.span.name": name,
            "theforge.start_ms": raw.get("start_ms"),
            "theforge.duration_ms": raw.get("duration_ms"),
            "theforge.schema": telemetry.get("schema", "theforge/RunTelemetry/v1"),
        }
        for key, value in (raw.get("attributes") or {}).items():
            attrs[f"theforge.attr.{key}"] = value
        if raw.get("parent") is not None:
            attrs["theforge.parent_span_id"] = raw["parent"]
        kind = SpanKind.INTERNAL
        if is_provider:
            kind = SpanKind.TOOL
            attrs["tool.name"] = f"theforge.{name}"
        err = raw.get("status") == "error"
        if err and isinstance(receipt.get("error"), dict):
            attrs["error.type"] = _bounded(receipt["error"].get("code"), 120)
            attrs["error.message"] = _bounded(receipt["error"].get("detail"))

        lens_name = name if is_provider else f"theforge.{name}"
        with tracer.start_span(lens_name, kind=kind, attributes=attrs) as s:
            s.provenance = Provenance.IMPORTED
            if base is not None:
                s.start_time = base + timedelta(
                    milliseconds=float(raw.get("start_ms") or 0))
            if err:
                detail = (receipt.get("error") or {}).get("detail") or "error"
                s.status = Status(
                    code=StatusCode.ERROR,
                    message=_bounded(
                        f"{(receipt.get('error') or {}).get('code', 'error')}: "
                        f"{detail}"),
                )
        # `finally` do SDK carimba end_time=now(); sobrescrevemos com a
        # duração REAL medida pelo theforge ancorada na base do run.
        if base is not None and s.start_time is not None:
            s.end_time = s.start_time + timedelta(
                milliseconds=float(raw.get("duration_ms") or 0))
        parent_ref = raw.get("parent")
        if parent_ref is not None and parent_ref in id_map:
            s.parent_span_id = id_map[parent_ref]
        id_map[tf_id] = s.span_id
        imported += 1

    summary: dict[str, Any] = {
        "run_id": telemetry.get("run_id"),
        "run_status": receipt.get("status"),
        "imported_spans": imported,
        "anchored_at": anchor_src,
        "error": receipt.get("error"),
        "unknowns": telemetry.get("unknowns") or [],
        "limitations": telemetry.get("limitations") or [],
    }
    # métricas agregadas reais do run — só as medidas; unknown nunca vira 0
    measured: dict[str, float] = {}
    for key, metric in telemetry.items():
        if isinstance(metric, dict) and metric.get("kind") == "measured":
            measured[key] = metric.get("value")
    summary["measured_metrics"] = measured
    sel = (routing.get("selected") or [{}])[0] if routing else {}
    summary["routing"] = {
        "status": routing.get("status"),
        "provider": sel.get("provider"),
        "capability": sel.get("capability"),
        "reason": routing.get("reason"),
    }
    if isinstance(result, dict) and result:
        summary["result"] = {
            "status": result.get("status"),
            "findings": len(result.get("findings") or []),
            "evidence": len(result.get("evidence") or []),
        }
    return summary


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def traced_theforge_ask(
    tracer: Any,
    theforge: list[str],
    ws: Path,
    env: dict[str, str],
    *,
    intent: str,
    use: str,
    capability: str | None = None,
    allow_unverified: bool = False,
    label: str,
) -> dict[str, Any]:
    """Um run real do theforge como trace: root OBSERVED (subprocess medido
    pelo harness) + filhos IMPORTED (telemetria nativa do theforge)."""
    attrs: dict[str, Any] = {
        "forge_lens.integration": "theforge",
        "forge_lens.provenance.note":
            "root=observed(subprocess wall-clock); children=imported"
            "(theforge/RunTelemetry/v1)",
        "theforge.workspace": ws.name,
    }
    with tracer.start_span(f"theforge {label}", kind=SpanKind.INTERNAL,
                           attributes=attrs) as root:
        out = run_ask(theforge, ws, env, intent, use=use,
                      capability=capability, allow_unverified=allow_unverified)
        root.attributes["process.exit_code"] = out["exit_code"]
        run_id = out.get("run_id")
        run_status = out.get("run_status") or "unknown"
        root.attributes["theforge.run_id"] = run_id
        root.attributes["theforge.run.status"] = run_status
        summary: dict[str, Any] = {"run_id": run_id, "run_status": run_status,
                                   "exit_code": out["exit_code"],
                                   "mode": "live"}
        if run_id:
            summary.update(import_run_spans(
                tracer, ws / ".forge" / "runs" / run_id))
            for key, value in (summary.get("measured_metrics") or {}).items():
                root.attributes[f"theforge.metric.{key}"] = value
            if summary.get("unknowns"):
                root.attributes["theforge.unknowns"] = ",".join(
                    summary["unknowns"])
        err = summary.get("error")
        if run_status != "ok" or out["exit_code"] != 0:
            root.status = Status(
                code=StatusCode.ERROR,
                message=_bounded(
                    f"run {run_status} (exit {out['exit_code']})"),
            )
            if isinstance(err, dict):
                root.attributes["theforge.error.code"] = _bounded(
                    err.get("code"), 120)
                root.attributes["theforge.error.detail"] = _bounded(
                    err.get("detail"))
        summary["trace_root_span_id"] = root.span_id
    return summary


def import_captured_run(tracer: Any, captured_dir: Path) -> dict[str, Any]:
    """Re-importa artefatos de um run REAL previamente gravado.

    Tudo sai provenance=IMPORTED — inclusive o root, cujos tempos vêm de
    ``receipt.started_at``/``finished_at`` (medidos pelo theforge, não por
    nós). Usado quando o sibling não está disponível no ambiente.
    """
    task = _read_json(captured_dir / "task.json")
    receipt = _read_json(captured_dir / "receipt.json")
    run_id = receipt.get("run_id") or task.get("id") or captured_dir.name
    started = _iso(receipt.get("started_at") or task.get("created_at"))
    finished = _iso(receipt.get("finished_at"))
    status = receipt.get("status") or "unknown"
    intent = _bounded(task.get("intent") or "captured run", 80)
    attrs: dict[str, Any] = {
        "forge_lens.integration": "theforge",
        "forge_lens.captured_fixture": True,
        "theforge.run_id": run_id,
        "theforge.run.status": status,
        "theforge.workspace": "<captured>",
        "forge_lens.provenance.note":
            "replay of a recorded real run; all spans imported",
    }
    # NOTA: tracer deve estar com auto_flush=False — os tempos do root são
    # corrigidos depois do `with` (o flush explícito leva os valores reais).
    with tracer.start_span(f"theforge ask [captured] {intent}",
                           kind=SpanKind.INTERNAL, attributes=attrs) as root:
        root.provenance = Provenance.IMPORTED
        summary = import_run_spans(tracer, captured_dir)
        for key, value in (summary.get("measured_metrics") or {}).items():
            root.attributes[f"theforge.metric.{key}"] = value
        err = summary.get("error")
        if status != "ok":
            root.status = Status(code=StatusCode.ERROR,
                                 message=_bounded(f"run {status}"))
            if isinstance(err, dict):
                root.attributes["theforge.error.code"] = _bounded(
                    err.get("code"), 120)
                root.attributes["theforge.error.detail"] = _bounded(
                    err.get("detail"))
    if started is not None:
        root.start_time = started
        root.end_time = finished or started
    summary.update({"run_id": run_id, "run_status": status, "mode": "captured"})
    return summary


# --------------------------------------------------------------------------
# servidor Forge Lens (uvicorn em thread, porta livre — mesmo padrão dos
# testes e2e do próprio forge-lens)
# --------------------------------------------------------------------------

@contextmanager
def lens_server(data_dir: Path) -> Iterator[dict[str, Any]]:
    """Sobe a API real (SQLite + uvicorn real socket) numa porta livre."""
    import uvicorn

    from forge_lens.api.app import create_app
    from forge_lens.core.config import LensConfig
    from forge_lens.storage.sqlite import SQLiteTraceStore

    lens_dir = data_dir / ".forge-lens"
    lens_dir.mkdir(parents=True, exist_ok=True)
    db = lens_dir / "lens.db"
    cfg = LensConfig(data_dir=lens_dir, db_path=db, project_id=PROJECT_ID)
    store = SQLiteTraceStore(db)
    app = create_app(store=store, config=cfg)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("uvicorn did not start")
        yield {
            "endpoint": f"http://127.0.0.1:{port}",
            "store": store,
            "db": db,
            "config": cfg,
        }
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        store.close()


# --------------------------------------------------------------------------
# cliente MCP stdio (JSON-RPC, uma linha por mensagem — protocolo real do
# `forge-lens mcp-serve`)
# --------------------------------------------------------------------------

class McpClient:
    """Fala com `python -m forge_lens mcp-serve --db <db>` via stdin/stdout."""

    def __init__(self, db: Path, *, cwd: Path | None = None) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "forge_lens", "mcp-serve",
             "--db", str(db)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=str(cwd) if cwd else None,
        )
        self._next_id = 0
        self.transcript: list[dict[str, Any]] = []

    def request(self, method: str, params: dict[str, Any] | None = None,
                *, timeout_s: float = 30.0) -> dict[str, Any]:
        self._next_id += 1
        rid = self._next_id
        req: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            req["params"] = params
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        assert self.proc.stdout is not None
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    f"mcp-serve closed stdout; stderr: {self._stderr()}")
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if resp.get("id") == rid:
                self.transcript.append({"request": req, "response": resp})
                return resp
        raise TimeoutError(f"mcp request {method} timed out")

    def notify(self, method: str) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        resp = self.request("tools/call", {"name": name, "arguments": args})
        result = resp.get("result") or {}
        content = result.get("content") or []
        text = content[0].get("text", "{}") if content else "{}"
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"_raw": text}
        return {"result_meta": {k: v for k, v in result.items()
                                if k != "content"},
                "payload": payload}

    def _stderr(self) -> str:
        if self.proc.stderr is None:
            return ""
        try:
            return self.proc.stderr.read()[-500:]
        except Exception:
            return ""

    def close(self) -> None:
        if self.proc.stdin:
            with contextlib.suppress(Exception):
                self.proc.stdin.close()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)

    def __enter__(self) -> McpClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


# --------------------------------------------------------------------------
# sanitização de evidência
# --------------------------------------------------------------------------

def sanitize_for_evidence(obj: Any, roots: dict[str, str]) -> Any:
    """Substitui caminhos absolutos por marcadores estáveis.

    ``roots`` mapeia marcador → prefixo absoluto (ex.: ``{"<repo>": "/home/…"}``).
    Nada além de path absolutos é reescrito: ids, timestamps e contagens são
    os valores reais gravados.
    """
    if isinstance(obj, str):
        out = obj
        for marker, prefix in sorted(roots.items(),
                                     key=lambda kv: -len(kv[1])):
            if prefix:
                out = out.replace(prefix, marker)
        return out
    if isinstance(obj, list):
        return [sanitize_for_evidence(v, roots) for v in obj]
    if isinstance(obj, dict):
        return {k: sanitize_for_evidence(v, roots) for k, v in obj.items()}
    return obj


def dump_json(path: Path, obj: Any, roots: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sanitize_for_evidence(obj, roots), indent=2,
                   sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return path


def http_get(url: str, timeout_s: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")  # noqa: S310 - loopback
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        return json.loads(resp.read().decode("utf-8"))
