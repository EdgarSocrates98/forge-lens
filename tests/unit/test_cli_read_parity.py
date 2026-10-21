"""CLI read-parity commands — Cycle 13 follow-on.

Covers the store/API read surfaces that previously had no CLI:
``capabilities``, ``stats``, ``datasets list|show``, the per-trace
analysis commands (``failure``/``loops``/``efficiency``/
``recommendations``/``security``/``replay``) and the ``import-otel``
file importer. Seeding follows the ``test_filters.py`` pattern: a
closed on-disk store the command reopens via ``--db``.
"""

from __future__ import annotations

import json
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


def _loop_trace() -> Trace:
    """Four identical tool calls — detect_loops must flag the run."""
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
        spans=spans,
    )


def _risky_trace() -> Trace:
    """A power-tool call — the security observer must flag it."""
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
    """Closed store on disk — each command reopens it via ``--db``.

    The chdir keeps any stray ``.forge-lens/config.toml`` or
    ``FORGE_LENS_*`` env from bending the resolved project_id.
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
        cases=[{"case_id": "c1"}, {"case_id": "c2"}],
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


# -- capabilities ------------------------------------------------------------


def test_capabilities_human_and_json() -> None:
    r = runner.invoke(app, ["capabilities"])
    assert r.exit_code == 0, r.output
    assert "claude_code" in r.output
    assert "forge" in r.output
    assert "coverage" in r.output

    r = runner.invoke(app, ["capabilities", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["hosts"]["claude_code"]["native_tracing"] is False
    assert out["hosts"]["forge"]["native_tracing"] is True
    assert "note" in out


# -- stats (mirrors /v1/ops/summary) ------------------------------------------


def test_stats_human_and_json(seeded_db: Path, synthetic_trace: Trace) -> None:
    r = runner.invoke(app, ["stats", "--db", str(seeded_db)])
    assert r.exit_code == 0, r.output
    assert "traces=3" in r.output
    assert "recent failures" in r.output
    assert synthetic_trace.trace_id in r.output

    r = runner.invoke(app, ["stats", "--db", str(seeded_db), "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert set(out) == {
        "project_id",
        "stats",
        "recent_traces",
        "recent_failures",
        "note",
    }
    assert out["project_id"] == "default"
    assert out["stats"]["traces"] == 3
    assert out["recent_traces"] == 3
    # only the synthetic trace ends in error
    assert out["recent_failures"] == [synthetic_trace.trace_id]


def test_stats_project_scope(seeded_db: Path) -> None:
    """--project scopes the roll-up exactly like the API's project_id."""
    r = runner.invoke(
        app, ["stats", "--db", str(seeded_db), "--project", "other", "--json"]
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["project_id"] == "other"
    assert out["stats"]["traces"] == 0
    assert out["recent_failures"] == []


# -- datasets -----------------------------------------------------------------


def test_datasets_list_empty(empty_db: Path) -> None:
    r = runner.invoke(app, ["datasets", "list", "--db", str(empty_db)])
    assert r.exit_code == 0, r.output
    assert "no datasets recorded" in r.output
    r = runner.invoke(
        app, ["datasets", "list", "--db", str(empty_db), "--json"]
    )
    assert json.loads(r.output) == []


def test_datasets_list_and_show(seeded_db: Path) -> None:
    r = runner.invoke(app, ["datasets", "list", "--db", str(seeded_db)])
    assert r.exit_code == 0, r.output
    assert "ds-1" in r.output
    assert "demo" in r.output

    r = runner.invoke(
        app, ["datasets", "list", "--db", str(seeded_db), "--json"]
    )
    items = json.loads(r.output)
    assert [d["dataset_id"] for d in items] == ["ds-1"]

    r = runner.invoke(
        app, ["datasets", "show", "ds-1", "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, r.output
    assert "demo" in r.output
    assert "answer questions" in r.output
    assert "v1" in r.output
    assert "2 case(s)" in r.output

    r = runner.invoke(
        app, ["datasets", "show", "ds-1", "--db", str(seeded_db), "--json"]
    )
    out = json.loads(r.output)
    assert out["dataset_id"] == "ds-1"
    assert out["metadata"]["owner"] == "evals"
    assert [v["version"] for v in out["versions"]] == ["v1"]
    assert out["versions"][0]["case_count"] == 2


def test_datasets_show_missing_exits_2(empty_db: Path) -> None:
    r = runner.invoke(
        app, ["datasets", "show", "nope", "--db", str(empty_db)]
    )
    assert r.exit_code == 2
    assert "not found" in r.output


# -- per-trace analysis --------------------------------------------------------


def test_failure_human_and_json(seeded_db: Path, synthetic_trace: Trace) -> None:
    r = runner.invoke(
        app, ["failure", synthetic_trace.trace_id, "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, r.output
    assert "failed=True" in r.output
    # synthetic_trace's tool span errors with "timeout"
    assert "timeout" in r.output

    r = runner.invoke(
        app,
        ["failure", synthetic_trace.trace_id, "--db", str(seeded_db), "--json"],
    )
    out = json.loads(r.output)
    assert out["trace_id"] == synthetic_trace.trace_id
    assert out["failed"] is True
    assert out["category"] == "timeout"
    assert out["evidence"]["span_ids"]


def test_loops_human_and_json(seeded_db: Path) -> None:
    r = runner.invoke(app, ["loops", LOOP_TID, "--db", str(seeded_db)])
    assert r.exit_code == 0, r.output
    assert "repeated_identical_tool" in r.output

    r = runner.invoke(
        app, ["loops", LOOP_TID, "--db", str(seeded_db), "--json"]
    )
    out = json.loads(r.output)
    assert out["trace_id"] == LOOP_TID
    kinds = {f["kind"] for f in out["findings"]}
    assert "repeated_identical_tool" in kinds
    # a clean trace reports no findings rather than inventing one
    clean = json.loads(
        runner.invoke(
            app, ["loops", RISKY_TID, "--db", str(seeded_db), "--json"]
        ).output
    )
    assert clean["findings"] == []


def test_loops_repeat_threshold_flag(seeded_db: Path) -> None:
    """--repeat-threshold mirrors the API's repeat_threshold param: the
    4-call run is a finding at the default of 3 but not at 5."""
    r = runner.invoke(
        app,
        ["loops", LOOP_TID, "--db", str(seeded_db),
         "--repeat-threshold", "5", "--json"],
    )
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["findings"] == []


def test_efficiency_human_and_json(seeded_db: Path, synthetic_trace: Trace) -> None:
    r = runner.invoke(
        app, ["efficiency", synthetic_trace.trace_id, "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, r.output
    assert "tools=1" in r.output
    assert "llm=1" in r.output

    r = runner.invoke(
        app,
        ["efficiency", synthetic_trace.trace_id, "--db", str(seeded_db),
         "--json"],
    )
    out = json.loads(r.output)
    assert out["trace_id"] == synthetic_trace.trace_id
    assert out["tool_calls"] == 1
    assert out["llm_calls"] == 1
    assert out["input_tokens"] == 100
    assert out["output_tokens"] == 42
    assert out["succeeded"] is False


def test_recommendations_human_and_json(seeded_db: Path) -> None:
    r = runner.invoke(
        app, ["recommendations", LOOP_TID, "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, r.output
    assert "redundant_tool_calls" in r.output

    r = runner.invoke(
        app, ["recommendations", LOOP_TID, "--db", str(seeded_db), "--json"]
    )
    out = json.loads(r.output)
    assert out["trace_id"] == LOOP_TID
    kinds = {rec["kind"] for rec in out["recommendations"]}
    assert "redundant_tool_calls" in kinds


def test_security_human_and_json(seeded_db: Path) -> None:
    r = runner.invoke(
        app, ["security", RISKY_TID, "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, r.output
    assert "risky_tool" in r.output

    r = runner.invoke(
        app, ["security", RISKY_TID, "--db", str(seeded_db), "--json"]
    )
    out = json.loads(r.output)
    assert out["trace_id"] == RISKY_TID
    assert out["mode"] == "observer"
    kinds = {o["kind"] for o in out["observations"]}
    assert "risky_tool" in kinds
    # the clean synthetic trace produces no observations
    r = runner.invoke(
        app, ["security", "a" * 32, "--db", str(seeded_db)]
    )
    assert "no security observations" in r.output


def test_replay_human_and_json(seeded_db: Path, synthetic_trace: Trace) -> None:
    r = runner.invoke(
        app, ["replay", synthetic_trace.trace_id, "--db", str(seeded_db)]
    )
    assert r.exit_code == 0, r.output
    assert "3 span(s)" in r.output
    assert "span_start" in r.output
    assert "span_end" in r.output

    r = runner.invoke(
        app,
        ["replay", synthetic_trace.trace_id, "--db", str(seeded_db), "--json"],
    )
    out = json.loads(r.output)
    assert out["trace_id"] == synthetic_trace.trace_id
    assert out["span_count"] == 3
    assert out["total_ms"] == 900
    assert [e["kind"] for e in out["entries"][:2]] == [
        "span_start",
        "span_start",
    ]


@pytest.mark.parametrize(
    "cmd",
    [
        ["failure"],
        ["loops"],
        ["efficiency"],
        ["recommendations"],
        ["security"],
        ["replay"],
    ],
)
def test_per_trace_commands_missing_trace_exit_2(
    seeded_db: Path, cmd: list[str]
) -> None:
    """A 404-style lookup is a clean message, never a traceback — same
    behavior for every per-trace analysis command."""
    r = runner.invoke(app, [*cmd, "f" * 32, "--db", str(seeded_db)])
    assert r.exit_code == 2, cmd
    assert "not found" in r.output
    assert "Traceback" not in r.output


# -- import-otel ---------------------------------------------------------------


def test_import_otel_round_trip(tmp_path: Path, otlp_payload: dict) -> None:
    """A real OTLP dump lands in the store queryable by the read
    commands — the same normalize → pipeline path as
    ``POST /v1/otlp/traces``."""
    db = tmp_path / "lens.db"
    f = tmp_path / "otlp.json"
    f.write_text(json.dumps(otlp_payload))
    r = runner.invoke(app, ["import-otel", str(f), "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "imported 1 trace(s), 2 span(s)" in r.output

    store = SQLiteTraceStore(db)
    try:
        trace = store.get_trace("b" * 32)
        assert trace is not None
        assert len(trace.spans) == 2
    finally:
        store.close()
    # and the stored trace is readable through the read-parity surface
    r = runner.invoke(app, ["traces", "list", "--db", str(db)])
    assert "b" * 32 in r.output


def test_import_otel_marks_provenance_imported(
    tmp_path: Path, otlp_payload: dict
) -> None:
    from forge_lens.contracts.provenance import Provenance

    db = tmp_path / "lens.db"
    f = tmp_path / "otlp.json"
    f.write_text(json.dumps(otlp_payload))
    r = runner.invoke(app, ["import-otel", str(f), "--db", str(db)])
    assert r.exit_code == 0, r.output
    store = SQLiteTraceStore(db)
    try:
        trace = store.get_trace("b" * 32)
        assert trace is not None
        assert trace.provenance == Provenance.IMPORTED
        assert all(
            s.provenance == Provenance.IMPORTED for s in trace.spans
        )
    finally:
        store.close()


def test_import_otel_malformed_json_exits_2(tmp_path: Path) -> None:
    f = tmp_path / "bad.json"
    f.write_text("{not json at all")
    r = runner.invoke(
        app, ["import-otel", str(f), "--db", str(tmp_path / "x.db")]
    )
    assert r.exit_code == 2
    assert "invalid JSON" in r.output
    assert "Traceback" not in r.output


def test_import_otel_missing_file_exits_2(tmp_path: Path) -> None:
    r = runner.invoke(
        app,
        ["import-otel", str(tmp_path / "nope.json"),
         "--db", str(tmp_path / "x.db")],
    )
    assert r.exit_code == 2
    assert "file not found" in r.output


def test_import_otel_non_object_exits_2(tmp_path: Path) -> None:
    """The API rejects non-object bodies 422 — the CLI answers exit 2."""
    f = tmp_path / "arr.json"
    f.write_text(json.dumps([1, 2, 3]))
    r = runner.invoke(
        app, ["import-otel", str(f), "--db", str(tmp_path / "x.db")]
    )
    assert r.exit_code == 2
    assert "JSON object" in r.output


def test_import_otel_quarantine_warnings(
    tmp_path: Path, otlp_payload: dict
) -> None:
    """A malformed span is quarantined + reported as a warning while the
    good span still imports — the batch is not rejected wholesale."""
    payload = otlp_payload
    spans: list[dict[str, Any]] = payload["resourceSpans"][0][
        "scopeSpans"
    ][0]["spans"]
    spans.append({"name": "no-ids"})  # missing traceId/spanId
    db = tmp_path / "lens.db"
    f = tmp_path / "otlp.json"
    f.write_text(json.dumps(payload))
    r = runner.invoke(app, ["import-otel", str(f), "--db", str(db)])
    assert r.exit_code == 0, r.output
    assert "imported 1 trace(s), 2 span(s)" in r.output
    assert "warning:" in r.output
    assert "quarantined" in r.output


def test_import_otel_enforces_payload_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cfg.max_payload_bytes bounds the file read exactly like the
    API's _read_capped_body."""
    cfg_dir = tmp_path / ".forge-lens"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text(
        "[lens]\nmax_payload_bytes = 64\n"
    )
    f = tmp_path / "big.json"
    f.write_text(json.dumps({"resourceSpans": [], "pad": "x" * 512}))
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(app, ["import-otel", str(f)])
    assert r.exit_code == 2
    assert "max_payload_bytes" in r.output
