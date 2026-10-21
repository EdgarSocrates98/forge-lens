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
