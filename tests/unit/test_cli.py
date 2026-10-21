"""CLI coverage via typer CliRunner — no subprocess, no real server."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from forge_lens.cli.main import app

runner = CliRunner()


def test_init_and_doctor(tmp_path: Path) -> None:
    result = runner.invoke(app, ["init", str(tmp_path), "--project", "t1"])
    assert result.exit_code == 0
    assert (tmp_path / ".forge-lens" / "config.toml").exists()

    result = runner.invoke(
        app, ["doctor", str(tmp_path), "--db", str(tmp_path / ".forge-lens" / "lens.db")]
    )
    assert result.exit_code == 0
    assert "[ok]" in result.output


def test_ingest_list_show(tmp_path: Path, synthetic_trace) -> None:
    db = tmp_path / "lens.db"
    envelope = {
        "schema_version": "1.0.0",
        "project_id": "cli-test",
        "traces": [synthetic_trace.model_dump(mode="json")],
    }
    f = tmp_path / "payload.json"
    f.write_text(json.dumps(envelope))

    r = runner.invoke(app, ["ingest", str(f), "--db", str(db), "--project", "cli-test"])
    assert r.exit_code == 0, r.output
    assert "ingested 3 spans" in r.output

    r = runner.invoke(app, ["traces", "list", "--db", str(db), "--project", "cli-test"])
    assert r.exit_code == 0
    assert synthetic_trace.trace_id in r.output

    r = runner.invoke(
        app,
        ["traces", "show", synthetic_trace.trace_id, "--db", str(db), "--project", "cli-test"],
    )
    assert r.exit_code == 0
    assert "llm gpt-test" in r.output
    assert "tool web_search" in r.output
    assert "input_tokens=100" in r.output
    # each span rendered exactly once
    assert r.output.count("llm gpt-test") == 1


def test_traces_show_not_found(tmp_path: Path) -> None:
    db = tmp_path / "lens.db"
    runner.invoke(app, ["init", str(tmp_path)])  # creates dirs
    r = runner.invoke(
        app, ["traces", "show", "0" * 32, "--db", str(db)]
    )
    assert r.exit_code == 1


def test_ingest_otlp_file(tmp_path: Path, otlp_payload: dict) -> None:
    db = tmp_path / "lens.db"
    f = tmp_path / "otlp.json"
    f.write_text(json.dumps(otlp_payload))
    r = runner.invoke(app, ["ingest", str(f), "--db", str(db), "--format", "otlp"])
    assert r.exit_code == 0
    assert "ingested 2 spans" in r.output


def test_version() -> None:
    r = runner.invoke(app, ["--version"])
    assert r.exit_code == 0
    assert r.output.strip() != ""


def test_traces_show_deep_tree_no_recursion_error(
    tmp_path: Path, synthetic_trace
) -> None:
    """A chain deeper than the recursion limit must render iteratively."""
    from datetime import timedelta

    base = synthetic_trace.spans[0]
    spans = []
    for i in range(1200):
        s = base.model_copy(deep=True)
        s.span_id = f"{i:016x}"
        s.parent_span_id = f"{i - 1:016x}" if i else None
        s.name = f"deep-{i}"
        s.start_time = base.start_time + timedelta(milliseconds=i)
        s.end_time = None
        spans.append(s)
    deep = synthetic_trace.model_copy(deep=True)
    deep.spans = spans

    db = tmp_path / "deep.db"
    from forge_lens.storage.sqlite import SQLiteTraceStore

    store = SQLiteTraceStore(db)
    store.upsert_trace(deep)
    store.close()

    r = runner.invoke(
        app, ["traces", "show", deep.trace_id, "--db", str(db), "--project", "default"]
    )
    assert r.exit_code == 0, r.output[:500]
    assert "deep-1199" in r.output  # tail of the chain rendered
    assert r.output.count("deep-0 ") == 1  # root rendered exactly once


def test_ingest_invalid_payload_exits_1(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text('{"traces": [{"bogus": true}]}')
    r = runner.invoke(app, ["ingest", str(f), "--db", str(tmp_path / "x.db")])
    assert r.exit_code == 1
    assert "ingest failed" in r.output


def test_serve_warns_on_exposed_bind(capsys) -> None:
    """--host 0.0.0.0 without api_key must warn before serving."""
    from forge_lens.cli.main import _warn_if_exposed
    from forge_lens.core.config import LensConfig

    _warn_if_exposed("0.0.0.0", LensConfig())  # noqa: S104
    assert "api_key" in capsys.readouterr().err

    # loopback bind: no warning
    _warn_if_exposed("127.0.0.1", LensConfig())
    assert capsys.readouterr().err == ""

    # non-loopback with a key configured: no warning
    _warn_if_exposed("0.0.0.0", LensConfig(api_key="k"))  # noqa: S104
    assert capsys.readouterr().err == ""


def test_ingest_rejects_unknown_format(tmp_path: Path) -> None:
    """--format garbage must fail loudly, not silently take the native path."""
    f = tmp_path / "t.json"
    f.write_text(json.dumps({"traces": []}))
    result = CliRunner().invoke(
        app, ["ingest", str(f), "--db", str(tmp_path / "x.db"), "--format", "yaml"]
    )
    assert result.exit_code == 2
    assert "unknown --format" in result.output


def test_cli_otlp_import_marks_provenance_imported(
    tmp_path: Path, otlp_payload: dict
) -> None:
    """File imports carry IMPORTED provenance — the store didn't observe
    the execution itself."""
    from forge_lens.contracts.provenance import Provenance
    from forge_lens.storage.sqlite import SQLiteTraceStore

    f = tmp_path / "otlp.json"
    f.write_text(json.dumps(otlp_payload))
    db = tmp_path / "cli.db"
    result = CliRunner().invoke(app, ["ingest", str(f), "--db", str(db)])
    assert result.exit_code == 0, result.output
    store = SQLiteTraceStore(db)
    trace = store.get_trace("b" * 32)
    assert trace is not None
    assert trace.provenance == Provenance.IMPORTED
    assert all(s.provenance == Provenance.IMPORTED for s in trace.spans)
    store.close()


def test_serve_binds_from_endpoint_config(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """cfg.endpoint is live config: serve derives its default bind from
    it, and --host/--port still override."""
    import uvicorn

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        uvicorn, "run",
        lambda app, host, port, **kw: calls.append((host, port)),
    )
    (tmp_path / ".forge-lens").mkdir()
    (tmp_path / ".forge-lens" / "config.toml").write_text(
        '[lens]\nendpoint = "http://10.1.2.3:8888"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_LENS_ENDPOINT", raising=False)

    result = CliRunner().invoke(app, ["serve", "--db", str(tmp_path / "x.db")])
    assert result.exit_code == 0, result.output
    assert calls == [("10.1.2.3", 8888)]

    # explicit flag wins over endpoint config
    result = CliRunner().invoke(
        app, ["serve", "--db", str(tmp_path / "x.db"), "--port", "9000"]
    )
    assert calls[-1] == ("10.1.2.3", 9000)


def test_serve_rejects_malformed_endpoint(tmp_path: Path, monkeypatch) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: None)
    (tmp_path / ".forge-lens").mkdir()
    (tmp_path / ".forge-lens" / "config.toml").write_text(
        '[lens]\nendpoint = "http://x:notaport"\n'
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FORGE_LENS_ENDPOINT", raising=False)
    result = CliRunner().invoke(app, ["serve", "--db", str(tmp_path / "x.db")])
    assert result.exit_code == 2
    assert "invalid endpoint" in result.output


def test_init_project_with_quotes_writes_valid_toml(tmp_path: Path) -> None:
    """A project_id containing quotes or control chars must not corrupt
    config.toml — the written file must load and round-trip the id."""
    from forge_lens.core.config import load_config

    r = runner.invoke(app, ["init", str(tmp_path), "--project", 'bad"name'])
    assert r.exit_code == 0, r.output
    cfg = load_config(tmp_path)  # TOMLDecodeError before the fix
    assert cfg.project_id == 'bad"name'

    sub = tmp_path / "p2"
    r = runner.invoke(app, ["init", str(sub), "--project", "a\nb\tc"])
    assert r.exit_code == 0, r.output
    assert load_config(sub).project_id == "a\nb\tc"


def test_ingest_missing_file_uses_formatted_error(tmp_path: Path) -> None:
    """File read/parse failures stay inside the `ingest failed:` path —
    a missing file must not produce a raw traceback."""
    r = runner.invoke(
        app,
        ["ingest", str(tmp_path / "nope.json"), "--db", str(tmp_path / "x.db")],
    )
    assert r.exit_code == 1
    assert "ingest failed" in r.output
    assert "Traceback" not in r.output


def test_ingest_malformed_json_uses_formatted_error(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("{not json at all")
    r = runner.invoke(app, ["ingest", str(f), "--db", str(tmp_path / "x.db")])
    assert r.exit_code == 1
    assert "ingest failed" in r.output
    assert "Traceback" not in r.output
