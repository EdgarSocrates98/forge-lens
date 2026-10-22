"""Homologação da CLI ``forge-lens`` — contrato de comandos, flags, saída
e códigos de saída exercitados via ``CliRunner`` (sem subprocess, sem
porta real, sem relógio).

Command table extraída de ``cli/main.py`` (todas as entradas cobertas
por ao menos um teste de sucesso ou de erro):

    --version · --help
    init <path> [--project]
    serve [--host H] [--port N] [--db] [--ssl-certfile F] [--ssl-keyfile F]
    ingest <file> [--db] [--format native|otlp|auto] [--project]
    import-claude <session.jsonl> [--db] [--project]
    import-otel <file> [--db] [--project]
    traces list [--db --project --status --session --tool --model --agent
                 --host --min-tokens --q --since --until
                 --min-duration-ms --max-duration-ms --limit --json]
    traces show <trace_id> [--db --project --json]
    graph|cost|incident|eval|failure|loops|efficiency|recommendations|
        correlate|security|replay <trace_id> [--db --project --json]
        (eval adds --budget-ms; loops adds --repeat-threshold)
    sessions|tools [--db --project --limit --json]
    stats [--db --project --json] · capabilities [--json]
    datasets list|show · keys create|list|revoke
    prune [--before|--days --project --db --yes]
    archive --before --out [--project --db --delete --append --yes]
    mcp-serve [--db] · install-status [--data-dir] · doctor [path] [--db]
    install [--data-dir --profile --source --file --dry-run]  (= apply)
    install status|doctor|uninstall [--data-dir]

Convenções homologadas (registradas também em ``docs/reference/cli.md`` →
"Homologação"):

    0  sucesso (inclui resultados vazios: "no traces stored" etc.)
    1  falha operacional genérica / abort de confirmação (typer.Abort)
    2  entrada inválida ou recurso inexistente — usage errors do typer,
       ``--format`` desconhecido, flags fora de faixa, config/env
       malformados, ``--db`` inabível, trace/dataset não encontrado

Ambiguidades deliberadamente NÃO alteradas (registro para o ciclo):

- ``keys revoke`` retorna 1 para "not found OR already revoked" — o
  store colapsa os dois casos num único bool; separar exigiria mudança
  em ``storage/sqlite.py`` (fora do escopo desta suíte).
- ``import-claude`` com arquivo lido mas sem eventos utilizáveis → 1
  (falha operacional "nothing imported", não entrada inválida).
- ``ingest`` agrupa TODO erro de arquivo/payload sob ``ingest failed:``
  + exit 1, enquanto ``import-otel`` separa entrada inválida (2) de
  falha de pipeline (1). Assimetria histórica preservada.
- ``sessions``/``tools`` clamam ``--limit`` a 0..500 (paridade com a
  API, que clama 1..500); ``traces list --limit`` rejeita negativos com
  exit 2 (contrato documentado) e aceita 0 → lista vazia.
- Grupo sem subcomando (``forge-lens traces``, ``keys``, ``datasets``,
  raiz) imprime help e sai 2 (comportamento click/typer para
  ``no_args_is_help``) — não é 0.
- ``install apply`` não existe: ``install`` sem subcomando JÁ é o apply
  (callback ``invoke_without_command``); ``install apply`` → exit 2.
- ``mcp_server.serve`` usa ``print(..., file=sys.stderr)`` — correto:
  stdout é o canal JSON-RPC do protocolo stdio. Arquivo fora do escopo.
- ``archive`` sem ``--project`` exporta TODOS os projetos
  (``project_id=None`` = wildcard no store) — amplo por design.
- ``--db`` com path inexistente CRIA um store novo (local-first);
  apenas paths inválidos (diretório, arquivo não-sqlite) → exit 2.
- ``eval --budget-ms`` negativo/NaN não falha: o avaliador reporta
  ``n/a`` com motivo — degradação evidenciada, por design.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from forge_lens.cli.main import app
from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.storage.sqlite import SQLiteTraceStore

runner = CliRunner()

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
LOOP_TID = "b" * 32
RISKY_TID = "c" * 32
REPO = Path(__file__).resolve().parents[2]
EXAMPLE_OTLP = REPO / "examples" / "sample-otlp.json"
FIXTURE_NATIVE = REPO / "tests" / "fixtures" / "native-ingest.json"

# every trace-id-taking command — the not-found contract must be uniform
TRACE_CMDS = [
    ["traces", "show"],
    ["graph"],
    ["cost"],
    ["incident"],
    ["eval"],
    ["failure"],
    ["loops"],
    ["efficiency"],
    ["recommendations"],
    ["correlate"],
    ["security"],
    ["replay"],
]

# --db-bearing commands for the unopenable-store sweep
DB_CMDS: list[list[str]] = [
    ["traces", "list"],
    ["traces", "show", "f" * 32],
    ["sessions"],
    ["tools"],
    ["stats"],
    ["failure", "f" * 32],
    ["graph", "f" * 32],
    ["datasets", "list"],
    ["keys", "list"],
]


def _loop_trace() -> Trace:
    """4 identical tool calls in one session — feeds loops/sessions."""
    spans = [
        Span(
            trace_id=LOOP_TID,
            span_id=f"{i + 1:016x}",
            name="tool read",
            kind=SpanKind.TOOL,
            start_time=_T0 + timedelta(milliseconds=i),
            end_time=_T0 + timedelta(milliseconds=i + 1),
            status=Status(code=StatusCode.OK),
            attributes={"tool.name": "read", "tool.args": {"path": "/x"}},
        )
        for i in range(4)
    ]
    return Trace(
        trace_id=LOOP_TID,
        root_span_id=spans[0].span_id,
        started_at=_T0,
        ended_at=_T0 + timedelta(milliseconds=4),
        status=Status(code=StatusCode.OK),
        session_id="sess-homo",
        spans=spans,
    )


def _risky_trace() -> Trace:
    """shell_exec on ../.env — the security observer must flag it."""
    span = Span(
        trace_id=RISKY_TID,
        span_id="1" * 16,
        name="tool shell_exec",
        kind=SpanKind.TOOL,
        start_time=_T0,
        end_time=_T0 + timedelta(milliseconds=10),
        status=Status(code=StatusCode.OK),
        attributes={
            "tool.name": "shell_exec",
            "tool.args": {"cmd": "cat ../.env"},
        },
    )
    return Trace(
        trace_id=RISKY_TID,
        root_span_id=span.span_id,
        started_at=_T0,
        ended_at=_T0 + timedelta(milliseconds=10),
        status=Status(code=StatusCode.OK),
        spans=[span],
    )


@pytest.fixture
def seeded_db(
    tmp_path: Path, synthetic_trace: Trace, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Closed on-disk store — commands reopen it via ``--db``.

    chdir + env scrub keeps repo-root ``.forge-lens/`` and ambient
    ``FORGE_LENS_*`` from bending the resolved project_id.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_LENS_PROJECT", raising=False)
    db = tmp_path / "lens.db"
    store = SQLiteTraceStore(db)
    store.upsert_trace(synthetic_trace)
    store.upsert_trace(_loop_trace())
    store.upsert_trace(_risky_trace())
    store.upsert_dataset(
        "ds-1", project_id="default", name="demo", task="answer questions",
        metadata={"owner": "evals"}, now_ns=1_000,
    )
    store.upsert_dataset_version(
        "ds-1", "v1", project_id="default", description="first",
        cases=[{"case_id": "c1"}],
    )
    store.close()
    return db


@pytest.fixture
def empty_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_LENS_PROJECT", raising=False)
    db = tmp_path / "empty.db"
    SQLiteTraceStore(db).close()
    return db


def _assert_invalid(res: Any, needle: str | None = None) -> None:
    """Invalid-input contract: exit 2, message on stderr, stdout clean."""
    assert res.exit_code == 2, res.output
    assert "Traceback" not in res.output
    if needle is not None:
        assert needle in res.stderr
        assert res.stdout == ""


# -- meta: help/version/usage -------------------------------------------------

_ALL_PATHS = [
    ["init"], ["serve"], ["ingest"], ["import-claude"], ["import-otel"],
    ["traces", "list"], ["traces", "show"], ["sessions"], ["tools"],
    ["graph"], ["cost"], ["capabilities"], ["stats"], ["failure"],
    ["loops"], ["efficiency"], ["recommendations"], ["correlate"],
    ["security"], ["replay"], ["incident"], ["eval"], ["doctor"],
    ["datasets", "list"], ["datasets", "show"],
    ["keys", "create"], ["keys", "list"], ["keys", "revoke"],
    ["prune"], ["archive"], ["mcp-serve"], ["install-status"],
    ["install"], ["install", "status"], ["install", "doctor"],
    ["install", "uninstall"],
]


@pytest.mark.parametrize("cmd", _ALL_PATHS)
def test_every_command_help_exits_0(cmd: list[str]) -> None:
    r = runner.invoke(app, [*cmd, "--help"])
    assert r.exit_code == 0, cmd
    assert "Traceback" not in r.output
    assert "Usage" in r.output


def test_root_help_and_version() -> None:
    r = runner.invoke(app, ["--help"])
    assert r.exit_code == 0
    for name in ("init", "serve", "ingest", "traces", "keys", "datasets",
                 "prune", "archive", "mcp-serve", "install", "doctor"):
        assert name in r.output
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert r.output.strip()


@pytest.mark.parametrize("cmd", [[], ["traces"], ["keys"], ["datasets"]])
def test_bare_group_shows_help_exit_2(cmd: list[str]) -> None:
    """no_args_is_help prints usage — observed contract is exit 2."""
    r = runner.invoke(app, cmd)
    assert r.exit_code == 2
    assert "Usage" in r.output


def test_unknown_command_and_flag_exit_2(empty_db: Path) -> None:
    r = runner.invoke(app, ["nonsense-cmd"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["traces", "list", "--db", str(empty_db),
                            "--bogus"])
    assert r.exit_code == 2
    r = runner.invoke(app, ["traces", "list", "--db"])  # missing value
    assert r.exit_code == 2


# -- init ----------------------------------------------------------------------


def test_init_creates_layout_and_is_idempotent(tmp_path: Path) -> None:
    r = runner.invoke(app, ["init", str(tmp_path), "--project", "p1"])
    assert r.exit_code == 0, r.output
    cfg = tmp_path / ".forge-lens" / "config.toml"
    assert cfg.exists()
    assert (tmp_path / ".forge-lens" / "artifacts").is_dir()
    assert "Initialized" in r.output

    # re-init never clobbers an existing config
    r = runner.invoke(app, ["init", str(tmp_path), "--project", "other"])
    assert r.exit_code == 0
    assert 'project_id = "p1"' in cfg.read_text()


# -- doctor ---------------------------------------------------------------------


def test_doctor_exit_semantics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    healthy = tmp_path / "proj"
    assert runner.invoke(app, ["init", str(healthy)]).exit_code == 0
    r = runner.invoke(app, ["doctor", str(healthy)])
    assert r.exit_code == 0, r.output
    assert "[ok]" in r.output
    assert "[FAIL]" not in r.output

    # never-initialized dir → FAIL check, exit 1 (not a crash)
    r = runner.invoke(app, ["doctor", str(tmp_path / "bare")])
    assert r.exit_code == 1
    assert "[FAIL] data dir exists" in r.output
    assert "Traceback" not in r.output


def test_doctor_malformed_config_reports_fail_not_crash(tmp_path: Path) -> None:
    """An unparseable config.toml is a doctor FAIL check, not a traceback."""
    data = tmp_path / ".forge-lens"
    data.mkdir()
    (data / "config.toml").write_text("not toml [[[")
    r = runner.invoke(app, ["doctor", str(tmp_path)])
    assert r.exit_code == 1
    assert "[FAIL] config parseable" in r.output
    assert "Traceback" not in r.output


# -- serve (never binds — uvicorn.run always mocked or unreached) ----------------


def test_serve_missing_uvicorn_exits_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    r = runner.invoke(app, ["serve", "--db", str(tmp_path / "x.db")])
    assert r.exit_code == 2
    assert "uvicorn not installed" in r.stderr


def test_serve_port_range_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Out-of-range --port is rejected before uvicorn ever runs."""
    import uvicorn

    monkeypatch.chdir(tmp_path)
    called: list[dict[str, Any]] = []
    monkeypatch.setattr(
        uvicorn, "run", lambda *a, **kw: called.append(kw))
    for bad in ("-1", "65536", "70000"):
        r = runner.invoke(
            app, ["serve", "--db", str(tmp_path / "x.db"), "--port", bad]
        )
        assert r.exit_code == 2, bad
        assert "invalid --port" in r.stderr
    assert called == []  # never reached the bind

    # port 0 = ephemeral bind is a legitimate value
    r = runner.invoke(
        app, ["serve", "--db", str(tmp_path / "x.db"), "--port", "0"]
    )
    assert r.exit_code == 0, r.output
    assert "ephemeral" in r.output
    assert called[0]["port"] == 0  # uvicorn.run(app, host=…, port=0)


def test_serve_happy_path_echoes_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    r = runner.invoke(
        app, ["serve", "--db", str(tmp_path / "x.db"), "--port", "7777"]
    )
    assert r.exit_code == 0, r.output
    assert "listening on http://127.0.0.1:7777" in r.output
    assert "POST /v1/otlp/traces" in r.output


# -- ingest / import ------------------------------------------------------------


def test_ingest_native_envelope(tmp_path: Path) -> None:
    db = tmp_path / "lens.db"
    r = runner.invoke(app, ["ingest", str(FIXTURE_NATIVE), "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "ingested 1 spans" in r.output
    # envelope project_id="fixture" rehomes the default-projected trace
    r = runner.invoke(app, ["traces", "list", "--db", str(db)])
    assert "no traces stored" in r.output
    r = runner.invoke(
        app, ["traces", "list", "--db", str(db), "--project", "fixture"]
    )
    assert "c" * 32 in r.output


def test_ingest_file_errors_stay_formatted(tmp_path: Path) -> None:
    r = runner.invoke(
        app, ["ingest", str(tmp_path / "nope.json"),
              "--db", str(tmp_path / "x.db")]
    )
    assert r.exit_code == 1
    assert "ingest failed" in r.stderr
    assert "Traceback" not in r.output

    f = tmp_path / "bad.json"
    f.write_text("{not json at all")
    r = runner.invoke(app, ["ingest", str(f), "--db", str(tmp_path / "x.db")])
    assert r.exit_code == 1
    assert "ingest failed" in r.stderr

    r = runner.invoke(
        app,
        ["ingest", str(FIXTURE_NATIVE), "--db", str(tmp_path / "x.db"),
         "--format", "yaml"],
    )
    _assert_invalid(r, "unknown --format")


def test_import_claude_round_trip_and_failures(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    f = tmp_path / "sess.jsonl"
    f.write_text(
        json.dumps(
            {
                "type": "tool_use",
                "tool_name": "read",
                "sessionId": "s1",
                "timestamp": "2026-01-01T10:00:00Z",
            }
        )
        + "\n"
    )
    r = runner.invoke(app, ["import-claude", str(f), "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "imported 1 span(s)" in r.output

    r = runner.invoke(
        app, ["import-claude", str(tmp_path / "nope.jsonl"), "--db", str(db)]
    )
    _assert_invalid(r, "file not found")

    empty = tmp_path / "empty.jsonl"
    empty.write_text("not json\n{}\n")
    r = runner.invoke(app, ["import-claude", str(empty), "--db", str(db)])
    assert r.exit_code == 1  # nothing usable — operational, not invalid
    assert "nothing imported" in r.stderr

    r = runner.invoke(
        app, ["import-claude", str(tmp_path), "--db", str(db)]
    )
    _assert_invalid(r, "cannot read")


def test_import_otel_repo_example(tmp_path: Path) -> None:
    """examples/sample-otlp.json — real checked-in fixture end to end."""
    db = tmp_path / "o.db"
    r = runner.invoke(app, ["import-otel", str(EXAMPLE_OTLP), "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "imported 1 trace(s), 3 span(s)" in r.output
    r = runner.invoke(
        app, ["traces", "show", "aaaabbbbccccddddeeeeffff00001111",
              "--db", str(db)]
    )
    assert r.exit_code == 0
    assert "agent-run" in r.output


def test_import_otel_input_errors(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    r = runner.invoke(
        app, ["import-otel", str(tmp_path / "nope.json"), "--db", str(db)]
    )
    _assert_invalid(r, "file not found")

    f = tmp_path / "bad.json"
    f.write_text("{not json")
    r = runner.invoke(app, ["import-otel", str(f), "--db", str(db)])
    _assert_invalid(r, "invalid JSON")

    f.write_text(json.dumps([1, 2, 3]))
    r = runner.invoke(app, ["import-otel", str(f), "--db", str(db)])
    _assert_invalid(r, "JSON object")


# -- traces list bounds ----------------------------------------------------------


def test_traces_list_success_paths(seeded_db: Path, synthetic_trace: Trace) -> None:
    r = runner.invoke(app, ["traces", "list", "--db", str(seeded_db)])
    assert r.exit_code == 0, r.output
    assert synthetic_trace.trace_id in r.output
    assert "errors=1" in r.output

    r = runner.invoke(
        app, ["traces", "list", "--db", str(seeded_db), "--json"]
    )
    items = json.loads(r.stdout)
    assert isinstance(items, list)
    assert len(items) == 3
    assert {i["trace_id"] for i in items} == {
        synthetic_trace.trace_id, LOOP_TID, RISKY_TID}

    # filters compose cleanly
    r = runner.invoke(
        app, ["traces", "list", "--db", str(seeded_db),
              "--status", "error", "--json"]
    )
    assert [i["trace_id"] for i in json.loads(r.stdout)] == [
        synthetic_trace.trace_id]
    r = runner.invoke(
        app, ["traces", "list", "--db", str(seeded_db),
              "--session", "sess-homo", "--json"]
    )
    assert [i["trace_id"] for i in json.loads(r.stdout)] == [LOOP_TID]
    r = runner.invoke(
        app, ["traces", "list", "--db", str(seeded_db),
              "--project", "other"]
    )
    assert "no traces stored" in r.output


@pytest.mark.parametrize(
    "args",
    [
        ["--limit", "-1"],
        ["--limit", str(2**63)],
        ["--since", "not-a-date"],
        ["--until", "2026-13-99"],
        ["--min-duration-ms", "-1"],
        ["--max-duration-ms", "nan"],
        ["--min-duration-ms", "inf"],
        ["--min-tokens", "-1"],
        ["--min-tokens", str(2**63)],
    ],
)
def test_traces_list_rejects_out_of_range(
    seeded_db: Path, args: list[str]
) -> None:
    r = runner.invoke(app, ["traces", "list", "--db", str(seeded_db), *args])
    _assert_invalid(r, "invalid")


def test_traces_list_limit_zero_and_huge_ok(seeded_db: Path) -> None:
    r = runner.invoke(
        app, ["traces", "list", "--db", str(seeded_db), "--limit", "0"]
    )
    assert r.exit_code == 0
    assert "no traces stored" in r.output  # limit 0 = list nothing

    r = runner.invoke(
        app, ["traces", "list", "--db", str(seeded_db),
              "--limit", str(2**63 - 1), "--json"]
    )
    assert r.exit_code == 0
    assert len(json.loads(r.stdout)) == 3


def test_traces_show(seeded_db: Path, synthetic_trace: Trace) -> None:
    r = runner.invoke(
        app, ["traces", "show", synthetic_trace.trace_id,
              "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, r.output
    assert "llm gpt-test" in r.output
    assert "!tool web_search" in r.output or "tool web_search" in r.output
    r = runner.invoke(
        app, ["traces", "show", synthetic_trace.trace_id,
              "--db", str(seeded_db), "--json"]
    )
    out = json.loads(r.stdout)
    assert out["trace_id"] == synthetic_trace.trace_id
    assert len(out["spans"]) == 3


# -- uniform not-found contract ---------------------------------------------------


@pytest.mark.parametrize("cmd", TRACE_CMDS)
def test_missing_trace_uniform_exit_2(seeded_db: Path, cmd: list[str]) -> None:
    """Every <trace_id> command answers a missing id identically:
    stderr 'trace <id> not found', exit 2, stdout empty."""
    r = runner.invoke(app, [*cmd, "f" * 32, "--db", str(seeded_db)])
    assert r.exit_code == 2, cmd
    assert r.stderr == f"trace {'f' * 32} not found\n"
    assert r.stdout == ""
    assert "Traceback" not in r.output


# -- roll-ups ---------------------------------------------------------------------


def test_sessions_and_tools(seeded_db: Path) -> None:
    r = runner.invoke(app, ["sessions", "--db", str(seeded_db)])
    assert r.exit_code == 0, r.output
    assert "sess-homo" in r.output
    r = runner.invoke(app, ["sessions", "--db", str(seeded_db), "--json"])
    items = json.loads(r.stdout)
    assert items[0]["session_id"] == "sess-homo"
    # negative --limit clamps (API parity) — divergent from traces list,
    # recorded in the module docstring
    r = runner.invoke(
        app, ["sessions", "--db", str(seeded_db), "--limit", "-5"])
    assert r.exit_code == 0
    assert "no sessions recorded" in r.output

    r = runner.invoke(app, ["tools", "--db", str(seeded_db)])
    assert r.exit_code == 0, r.output
    assert "web_search" in r.output
    assert "read" in r.output
    r = runner.invoke(app, ["tools", "--db", str(seeded_db), "--json"])
    tools = {t["tool"] for t in json.loads(r.stdout)}
    assert {"web_search", "read", "shell_exec"} <= tools


def test_stats_json_contract(seeded_db: Path) -> None:
    r = runner.invoke(app, ["stats", "--db", str(seeded_db), "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.stdout)
    assert out["stats"]["traces"] == 3
    assert out["recent_traces"] == 3
    r = runner.invoke(app, ["stats", "--db", str(seeded_db)])
    assert "traces=3" in r.output


def test_capabilities_no_store_needed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["capabilities", "--json"])
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert "hosts" in out
    assert "note" in out
    r = runner.invoke(app, ["capabilities"])
    assert r.exit_code == 0
    assert "coverage" in r.output


# -- per-trace analysis, success + json -------------------------------------------


@pytest.mark.parametrize("cmd", TRACE_CMDS[1:])  # traces show covered above
def test_analysis_commands_json_parse(
    seeded_db: Path, synthetic_trace: Trace, cmd: list[str]
) -> None:
    r = runner.invoke(
        app, [*cmd, synthetic_trace.trace_id, "--db", str(seeded_db),
              "--json"]
    )
    assert r.exit_code == 0, (cmd, r.output)
    out = json.loads(r.stdout)
    # every per-trace payload names the subject trace — graph calls it
    # subject_id (ExecutionGraph contract), eval emits a result list
    assert isinstance(out, (dict, list))
    if isinstance(out, dict):
        key = "subject_id" if cmd == ["graph"] else "trace_id"
        assert out[key] == synthetic_trace.trace_id


@pytest.mark.parametrize("cmd", TRACE_CMDS[1:])
def test_analysis_commands_human_render(
    seeded_db: Path, synthetic_trace: Trace, cmd: list[str]
) -> None:
    r = runner.invoke(
        app, [*cmd, synthetic_trace.trace_id, "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, (cmd, r.output)
    assert r.output.strip()
    assert "Traceback" not in r.output


def test_failure_and_loops_semantics(seeded_db: Path) -> None:
    r = runner.invoke(
        app, ["loops", LOOP_TID, "--db", str(seeded_db), "--json"])
    assert r.exit_code == 0
    kinds = {f["kind"] for f in json.loads(r.stdout)["findings"]}
    assert "repeated_identical_tool" in kinds

    r = runner.invoke(
        app, ["security", RISKY_TID, "--db", str(seeded_db)])
    assert r.exit_code == 0
    assert "risky_tool" in r.output


def test_eval_budget_flag(seeded_db: Path, synthetic_trace: Trace) -> None:
    r = runner.invoke(
        app, ["eval", synthetic_trace.trace_id, "--db", str(seeded_db),
              "--budget-ms", "1000", "--json"]
    )
    assert r.exit_code == 0, r.output
    names = {e["evaluator"] for e in json.loads(r.stdout)}
    assert {"error_free", "tool_success", "latency_budget"} <= names
    # a nonsense budget degrades to n/a, never a crash
    r = runner.invoke(
        app, ["eval", synthetic_trace.trace_id, "--db", str(seeded_db),
              "--budget-ms", "-5"]
    )
    assert r.exit_code == 0
    assert "n/a" in r.output


# -- datasets / keys ----------------------------------------------------------------


def test_datasets_list_show(seeded_db: Path) -> None:
    r = runner.invoke(app, ["datasets", "list", "--db", str(seeded_db)])
    assert r.exit_code == 0
    assert "ds-1" in r.output
    r = runner.invoke(
        app, ["datasets", "show", "ds-1", "--db", str(seeded_db), "--json"])
    out = json.loads(r.stdout)
    assert out["dataset_id"] == "ds-1"
    assert out["versions"][0]["version"] == "v1"
    r = runner.invoke(
        app, ["datasets", "show", "nope", "--db", str(seeded_db)])
    _assert_invalid(r, "not found")


def test_datasets_list_empty(empty_db: Path) -> None:
    r = runner.invoke(app, ["datasets", "list", "--db", str(empty_db)])
    assert r.exit_code == 0
    assert "no datasets recorded" in r.output
    assert json.loads(
        runner.invoke(
            app, ["datasets", "list", "--db", str(empty_db), "--json"]
        ).stdout
    ) == []


def test_keys_lifecycle(tmp_path: Path) -> None:
    db = tmp_path / "k.db"
    r = runner.invoke(app, ["keys", "create", "--name", "ci",
                            "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "secret:" in r.output  # shown once — only the hash persists
    key_id = next(
        ln.split()[-1] for ln in r.output.splitlines()
        if ln.startswith("key_id:")
    )

    r = runner.invoke(app, ["keys", "list", "--db", str(db)])
    assert r.exit_code == 0
    assert key_id in r.output
    assert "active" in r.output

    r = runner.invoke(app, ["keys", "revoke", key_id, "--db", str(db)])
    assert r.exit_code == 0
    # second revoke → "not found or already revoked" (conflated 1)
    r = runner.invoke(app, ["keys", "revoke", key_id, "--db", str(db)])
    assert r.exit_code == 1
    assert "not found or already revoked" in r.stderr
    r = runner.invoke(app, ["keys", "list", "--db", str(db)])
    assert "revoked" in r.output


def test_keys_input_validation(tmp_path: Path) -> None:
    db = tmp_path / "k.db"
    r = runner.invoke(app, ["keys", "create", "--db", str(db)])
    assert r.exit_code == 2  # required --name missing → usage error
    r = runner.invoke(
        app, ["keys", "create", "--name", "x", "--role", "root",
              "--db", str(db)]
    )
    _assert_invalid(r, "invalid role")
    r = runner.invoke(app, ["keys", "revoke", "--db", str(db)])
    assert r.exit_code == 2  # missing positional arg


# -- unopenable store sweep ---------------------------------------------------------


@pytest.mark.parametrize("cmd", DB_CMDS)
def test_db_pointing_at_directory_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cmd: list[str]
) -> None:
    """--db naming a directory → 'cannot open store' + exit 2,
    never a driver traceback — uniform across store commands."""
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, [*cmd, "--db", str(tmp_path)])
    _assert_invalid(r, "cannot open store")


def test_db_pointing_at_non_sqlite_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    bogus = tmp_path / "notadb.db"
    bogus.write_text("hello")
    r = runner.invoke(app, ["traces", "list", "--db", str(bogus)])
    _assert_invalid(r, "cannot open store")


def test_malformed_env_is_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FORGE_LENS_MAX_PAYLOAD_BYTES", "abc")
    r = runner.invoke(app, ["traces", "list", "--db", str(tmp_path / "x.db")])
    _assert_invalid(r, "invalid configuration")
    assert "FORGE_LENS_MAX_PAYLOAD_BYTES" in r.stderr


# -- prune / archive ----------------------------------------------------------------


def test_prune_argument_validation(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _assert_invalid(
        runner.invoke(app, ["prune", "--db", str(db)]), "no cutoff")
    _assert_invalid(
        runner.invoke(app, ["prune", "--db", str(db), "--before",
                            "2030-01-01", "--days", "1"]),
        "exclusive")
    _assert_invalid(
        runner.invoke(app, ["prune", "--db", str(db), "--days", "-1"]),
        "non-negative")
    _assert_invalid(
        runner.invoke(app, ["prune", "--db", str(db), "--before", "nope"]),
        "invalid --before")


def test_prune_empty_and_confirm_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty_db: Path
) -> None:
    r = runner.invoke(
        app, ["prune", "--db", str(empty_db), "--days", "0", "--yes"])
    assert r.exit_code == 0
    assert "pruned 0 trace(s)" in r.output

    # declining the confirmation aborts (exit 1), deletes nothing
    r = runner.invoke(
        app, ["prune", "--db", str(empty_db), "--days", "0"], input="n\n")
    assert r.exit_code == 1
    assert "Aborted" in r.output


def test_prune_deletes_old_traces(
    seeded_db: Path, synthetic_trace: Trace
) -> None:
    r = runner.invoke(
        app, ["prune", "--db", str(seeded_db), "--before", "2030-01-01",
              "--yes"])
    assert r.exit_code == 0, r.output
    assert "pruned 3 trace(s)" in r.output
    r = runner.invoke(app, ["traces", "list", "--db", str(seeded_db)])
    assert "no traces stored" in r.output


def test_archive_requires_before_and_out(tmp_path: Path) -> None:
    r = runner.invoke(app, ["archive", "--db", str(tmp_path / "x.db")])
    assert r.exit_code == 2  # missing required options → usage error


def test_archive_empty_store_and_overwrite_guard(
    tmp_path: Path, empty_db: Path
) -> None:
    out = tmp_path / "archive.ndjson"
    r = runner.invoke(
        app, ["archive", "--db", str(empty_db), "--before", "2030-01-01",
              "--out", str(out)])
    assert r.exit_code == 0, r.output
    assert "archived 0 trace(s)" in r.output
    assert out.exists()
    assert out.read_text() == ""

    # refusing to clobber a prior archive without --append
    r = runner.invoke(
        app, ["archive", "--db", str(empty_db), "--before", "2030-01-01",
              "--out", str(out)])
    _assert_invalid(r, "refusing to truncate")
    r = runner.invoke(
        app, ["archive", "--db", str(empty_db), "--before", "2030-01-01",
              "--out", str(out), "--append"])
    assert r.exit_code == 0


def test_archive_exports_then_deletes(seeded_db: Path, tmp_path: Path) -> None:
    out = tmp_path / "a.ndjson"
    r = runner.invoke(
        app, ["archive", "--db", str(seeded_db), "--before", "2030-01-01",
              "--out", str(out)])
    assert r.exit_code == 0
    assert "archived 3 trace(s)" in r.output
    lines = out.read_text().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["trace_id"]

    # nothing older than a past cutoff → idempotent no-op
    r = runner.invoke(
        app, ["archive", "--db", str(seeded_db), "--before", "2020-01-01",
              "--out", str(tmp_path / "b.ndjson")])
    assert "archived 0 trace(s)" in r.output

    # --delete without --yes aborts; with --yes prunes only exported ids
    r = runner.invoke(
        app, ["archive", "--db", str(seeded_db), "--before", "2030-01-01",
              "--out", str(tmp_path / "c.ndjson"), "--delete"], input="n\n")
    assert r.exit_code == 1
    r = runner.invoke(
        app, ["archive", "--db", str(seeded_db), "--before", "2030-01-01",
              "--out", str(tmp_path / "d.ndjson"), "--delete", "--yes"])
    assert r.exit_code == 0
    assert "pruned 3 trace(s)" in r.output
    r = runner.invoke(app, ["traces", "list", "--db", str(seeded_db)])
    assert "no traces stored" in r.output


# -- mcp-serve ---------------------------------------------------------------------


def test_mcp_serve_missing_db_exits_2(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["mcp-serve", "--db", str(tmp_path / "nope.db")])
    assert r.exit_code == 2
    assert "cannot open store" in r.stderr
    assert "Traceback" not in r.output


def test_mcp_serve_eof_exits_cleanly(seeded_db: Path) -> None:
    """stdin at EOF → the stdio loop returns instead of hanging."""
    r = runner.invoke(app, ["mcp-serve", "--db", str(seeded_db)], input="")
    assert r.exit_code == 0


# -- install lifecycle (smoke — full coverage lives in test_cli.py) ------------------


def test_install_verbs_smoke(tmp_path: Path) -> None:
    d = tmp_path / "data"
    r = runner.invoke(
        app, ["install", "--data-dir", str(d), "--dry-run"])
    assert r.exit_code == 0
    assert json.loads(r.stdout)["dry_run"] is True
    assert not d.exists()

    r = runner.invoke(app, ["install", "status", "--data-dir", str(d)])
    assert r.exit_code == 0
    assert json.loads(r.stdout)["installed"] is False

    # nothing installed → health check fails honestly
    r = runner.invoke(app, ["install", "doctor", "--data-dir", str(d)])
    assert r.exit_code == 1
    assert json.loads(r.stdout)["ok"] is False

    r = runner.invoke(
        app, ["install", "--data-dir", str(d), "--profile", "minimal"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["install", "status", "--data-dir", str(d)])
    assert json.loads(r.stdout)["installed"] is True

    r = runner.invoke(app, ["install", "uninstall", "--data-dir", str(d)])
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert "removed" in out
    assert "prior_receipt" in out
    r = runner.invoke(app, ["install", "status", "--data-dir", str(d)])
    assert json.loads(r.stdout)["receipt"]["action"] == "uninstall"


def test_install_apply_is_not_a_subcommand(tmp_path: Path) -> None:
    """The bare `install` callback IS apply — `install apply` is an
    unknown command, not an alias."""
    r = runner.invoke(app, ["install", "apply", "--data-dir", str(tmp_path)])
    assert r.exit_code == 2
    assert "No such command" in r.output


def test_install_status_legacy_verb(tmp_path: Path) -> None:
    r = runner.invoke(app, ["install-status", "--data-dir", str(tmp_path)])
    assert r.exit_code == 0
    out = json.loads(r.stdout)
    assert set(out) == {"status", "doctor"}
