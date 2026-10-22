"""CLI surface coverage — analyze/audit/experiments/alerts/eval-suggest/
intel, plus `traces list --offset` and `eval --evaluator/--params-json`.

Every command talks to a real seeded SQLiteTraceStore through the same
helpers the API uses — no HTTP, no sockets, no mocks of the store.
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

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _trace(tid: str, *, tool_error: bool = True) -> Trace:
    """Deterministic small trace: agent → llm + tool (optionally failing)."""
    agent = Span(
        trace_id=tid,
        span_id="1" * 16,
        name="agent.run",
        kind=SpanKind.AGENT,
        start_time=T0,
        end_time=T0 + timedelta(milliseconds=900),
        status=Status(code=StatusCode.OK),
    )
    llm = Span(
        trace_id=tid,
        span_id="2" * 16,
        parent_span_id="1" * 16,
        name="llm gpt-test",
        kind=SpanKind.LLM,
        start_time=T0 + timedelta(milliseconds=50),
        end_time=T0 + timedelta(milliseconds=400),
        attributes={
            "llm.model_name": "gpt-test",
            "forge_lens.token_usage": {
                "provider": "openai",
                "model": "gpt-test",
                "input_tokens": 100,
                "output_tokens": 42,
                "total_tokens": 142,
                "measurement_source": "provider",
            },
        },
        status=Status(code=StatusCode.OK),
    )
    tool = Span(
        trace_id=tid,
        span_id="3" * 16,
        parent_span_id="1" * 16,
        name="tool shell",
        kind=SpanKind.TOOL,
        start_time=T0 + timedelta(milliseconds=420),
        end_time=T0 + timedelta(milliseconds=800),
        attributes={"tool.name": "shell"},
        status=Status(
            code=StatusCode.ERROR if tool_error else StatusCode.OK,
            message="boom" if tool_error else None,
        ),
    )
    return Trace(
        trace_id=tid,
        root_span_id="1" * 16,
        started_at=T0,
        ended_at=T0 + timedelta(milliseconds=900),
        status=Status(code=StatusCode.ERROR if tool_error else StatusCode.OK),
        spans=[agent, llm, tool],
    )


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """Three traces (2 failing, 1 ok) + one audit event in a real store."""
    db = tmp_path / "lens.db"
    store = SQLiteTraceStore(db)
    try:
        store.upsert_trace(_trace("a" * 32))
        store.upsert_trace(_trace("b" * 32))
        store.upsert_trace(_trace("c" * 32, tool_error=False))
        store.record_audit(
            actor="cli",
            action="traces.prune",
            resource="*",
            outcome="ok",
            ts_ns=1_700_000_000_000_000_000,
        )
    finally:
        store.close()
    return db


def _invoke(args: list[str], db: Path | None = None) -> Any:
    full = list(args)
    if db is not None:
        full += ["--db", str(db)]
    return runner.invoke(app, full)


# -- traces list --offset ---------------------------------------------------


def test_traces_list_offset(seeded_db: Path) -> None:
    all_ids = json.loads(
        _invoke(["traces", "list", "--json"], seeded_db).output
    )
    assert len(all_ids) == 3
    page = _invoke(["traces", "list", "--offset", "2", "--json"], seeded_db)
    assert page.exit_code == 0, page.output
    ids = [r["trace_id"] for r in json.loads(page.output)]
    assert ids == [r["trace_id"] for r in all_ids[2:]]


def test_traces_list_offset_rejects_bad_values(seeded_db: Path) -> None:
    for bad in ("-1", str(2**63)):
        r = _invoke(["traces", "list", "--offset", bad], seeded_db)
        assert r.exit_code == 2
        assert "--offset" in r.output


# -- analyze ----------------------------------------------------------------


def test_analyze_list_shows_all_specialists() -> None:
    from forge_lens.ops.agents import SPECIALISTS

    r = runner.invoke(app, ["analyze", "--list"])
    assert r.exit_code == 0
    for name in SPECIALISTS:
        assert name in r.output
    assert len(SPECIALISTS) == 11

    r_json = runner.invoke(app, ["analyze", "--list", "--json"])
    assert r_json.exit_code == 0
    rows = json.loads(r_json.output)
    assert {row["name"] for row in rows} == set(SPECIALISTS)
    assert all(row["description"] for row in rows)


def test_analyze_happy_path(seeded_db: Path) -> None:
    r = _invoke(
        ["analyze", "trace_investigator", "a" * 32, "--json"], seeded_db
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["specialist"] == "trace_investigator"
    assert out["stop_reason"] == "completed"
    assert out["iterations"] >= 1
    assert any(f.get("summary") == "3 spans" for f in out["findings"])


def test_analyze_unknown_specialist_lists_valid(seeded_db: Path) -> None:
    r = _invoke(["analyze", "nosuch", "a" * 32], seeded_db)
    assert r.exit_code == 2
    assert "trace_investigator" in r.output
    assert "unknown specialist" in r.output


def test_analyze_missing_trace_exits_2(seeded_db: Path) -> None:
    r = _invoke(["analyze", "trace_investigator", "0" * 32], seeded_db)
    assert r.exit_code == 2


def test_analyze_missing_args_usage_error(seeded_db: Path) -> None:
    r = _invoke(["analyze"], seeded_db)
    assert r.exit_code == 2


# -- audit ------------------------------------------------------------------


def test_audit_empty_is_graceful(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    SQLiteTraceStore(db).close()
    r = _invoke(["audit"], db)
    assert r.exit_code == 0
    assert "no audit events" in r.output
    r_json = _invoke(["audit", "--json"], db)
    assert json.loads(r_json.output) == []


def test_audit_rows_and_filters(seeded_db: Path) -> None:
    r = _invoke(["audit"], seeded_db)
    assert r.exit_code == 0
    assert "traces.prune" in r.output
    assert "cli" in r.output

    r_json = _invoke(["audit", "--json"], seeded_db)
    rows = json.loads(r_json.output)
    assert rows[0]["action"] == "traces.prune"
    assert rows[0]["actor"] == "cli"

    hit = _invoke(["audit", "--action", "traces.prune", "--json"], seeded_db)
    assert len(json.loads(hit.output)) == 1
    miss = _invoke(["audit", "--actor", "nobody", "--json"], seeded_db)
    assert json.loads(miss.output) == []


def test_audit_rejects_bad_paging(seeded_db: Path) -> None:
    for args in (["--limit", "-1"], ["--limit", "501"], ["--offset", "-1"]):
        r = _invoke(["audit", *args], seeded_db)
        assert r.exit_code == 2


# -- experiments -------------------------------------------------------------

_EXP_COHORTS = json.dumps(
    {"a": {"trace_ids": ["a" * 32]}, "b": {"trace_ids": ["c" * 32]}}
)


def test_experiments_create_list_show(seeded_db: Path) -> None:
    r = _invoke(
        [
            "experiments", "create", "exp1",
            "--name", "smoke",
            "--evaluators-json", '[{"name": "error_free"}]',
            "--cohorts-json", _EXP_COHORTS,
        ],
        seeded_db,
    )
    assert r.exit_code == 0, r.output

    r = _invoke(["experiments", "list", "--json"], seeded_db)
    assert r.exit_code == 0
    exps = json.loads(r.output)
    assert [e["experiment_id"] for e in exps] == ["exp1"]
    assert exps[0]["name"] == "smoke"

    r = _invoke(["experiments", "show", "exp1", "--json"], seeded_db)
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert out["experiment_id"] == "exp1"
    assert out["cohorts"]["a"]["trace_ids"] == ["a" * 32]
    assert out["runs"] == []


def test_experiments_create_validation(seeded_db: Path) -> None:
    bad_specs = [
        "{not json",
        "[]",
        '[{"no_name": 1}]',
        '"string"',
    ]
    for spec in bad_specs:
        r = _invoke(
            [
                "experiments", "create", "bad",
                "--evaluators-json", spec,
                "--cohorts-json", _EXP_COHORTS,
            ],
            seeded_db,
        )
        assert r.exit_code == 2, spec

    bad_cohorts = [
        "{not json",
        "{}",
        '{"a": {"trace_ids": []}, "b": {"trace_ids": ["x"]}}',
        '{"a": {"trace_ids": [1]}, "b": {"trace_ids": ["x"]}}',
        '{"a": {}, "b": {"trace_ids": ["x"]}}',
    ]
    for cohorts in bad_cohorts:
        r = _invoke(
            [
                "experiments", "create", "bad",
                "--evaluators-json", '[{"name": "error_free"}]',
                "--cohorts-json", cohorts,
            ],
            seeded_db,
        )
        assert r.exit_code == 2, cohorts

    # --dataset-version requires --dataset-id
    r = _invoke(
        [
            "experiments", "create", "bad",
            "--dataset-version", "v1",
            "--evaluators-json", '[{"name": "error_free"}]',
            "--cohorts-json", _EXP_COHORTS,
        ],
        seeded_db,
    )
    assert r.exit_code == 2

    # --metadata-json must be an object
    r = _invoke(
        [
            "experiments", "create", "bad",
            "--metadata-json", "[1]",
            "--evaluators-json", '[{"name": "error_free"}]',
            "--cohorts-json", _EXP_COHORTS,
        ],
        seeded_db,
    )
    assert r.exit_code == 2


def test_experiments_run_persists_result(seeded_db: Path) -> None:
    _invoke(
        [
            "experiments", "create", "exp1",
            "--evaluators-json",
            '[{"name": "error_free"}, {"name": "tool_success"}]',
            "--cohorts-json", _EXP_COHORTS,
        ],
        seeded_db,
    )
    r = _invoke(["experiments", "run", "exp1", "--json"], seeded_db)
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["run_id"] == 1
    assert out["report"]["deltas"]
    assert out["report"]["a"]["trace_ids"] == ["a" * 32]
    assert out["report"]["b"]["trace_ids"] == ["c" * 32]

    shown = json.loads(
        _invoke(["experiments", "show", "exp1", "--json"], seeded_db).output
    )
    assert len(shown["runs"]) == 1
    assert shown["runs"][0]["report"] == out["report"]


def test_experiments_run_gate(seeded_db: Path) -> None:
    _invoke(
        [
            "experiments", "create", "exp1",
            "--evaluators-json", '[{"name": "error_free"}]',
            "--cohorts-json", _EXP_COHORTS,
        ],
        seeded_db,
    )
    r = _invoke(
        [
            "experiments", "run", "exp1",
            "--gate-json", '{"max_regressions": 0}',
            "--json",
        ],
        seeded_db,
    )
    assert r.exit_code == 0, r.output
    assert "gate" in json.loads(r.output)["report"]


def test_experiments_run_missing(seeded_db: Path) -> None:
    r = _invoke(["experiments", "run", "nope"], seeded_db)
    assert r.exit_code == 2
    r = _invoke(["experiments", "show", "nope"], seeded_db)
    assert r.exit_code == 2


def test_experiments_run_missing_cohort_trace(seeded_db: Path) -> None:
    _invoke(
        [
            "experiments", "create", "ghost",
            "--evaluators-json", '[{"name": "error_free"}]',
            "--cohorts-json",
            json.dumps(
                {
                    "a": {"trace_ids": ["0" * 32]},
                    "b": {"trace_ids": ["c" * 32]},
                }
            ),
        ],
        seeded_db,
    )
    r = _invoke(["experiments", "run", "ghost"], seeded_db)
    assert r.exit_code == 2
    assert "not found" in r.output


def test_experiments_run_bad_gate_json(seeded_db: Path) -> None:
    _invoke(
        [
            "experiments", "create", "exp1",
            "--evaluators-json", '[{"name": "error_free"}]',
            "--cohorts-json", _EXP_COHORTS,
        ],
        seeded_db,
    )
    r = _invoke(
        ["experiments", "run", "exp1", "--gate-json", "{bad"], seeded_db
    )
    assert r.exit_code == 2
    r = _invoke(
        ["experiments", "run", "exp1", "--gate-json", "[1]"], seeded_db
    )
    assert r.exit_code == 2


# -- alerts ------------------------------------------------------------------

_RULE = {
    "rule_id": "err-rate",
    "metric": "error_rate",
    "op": ">",
    "threshold": 0.1,
    "severity": "critical",
}


def test_alerts_lifecycle(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # alerts commands resolve data_dir from cwd — isolate in tmp_path
    monkeypatch.chdir(tmp_path)

    r = runner.invoke(app, ["alerts", "list"])
    assert r.exit_code == 0
    assert "no alert rules" in r.output

    r = runner.invoke(
        app, ["alerts", "save", "--rule-json", json.dumps(_RULE)]
    )
    assert r.exit_code == 0, r.output
    rules_file = tmp_path / ".forge-lens" / "alert-rules.json"
    assert rules_file.exists()

    r = runner.invoke(app, ["alerts", "list", "--json"])
    assert r.exit_code == 0
    rules = json.loads(r.output)
    assert [x["rule_id"] for x in rules] == ["err-rate"]

    r = runner.invoke(
        app, ["alerts", "evaluate", "--db", str(seeded_db), "--json"]
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["evaluated"] == 1
    assert len(out["fired"]) == 1
    assert out["fired"][0]["rule_id"] == "err-rate"
    assert out["project_id"] == "default"

    r = runner.invoke(app, ["alerts", "delete", "err-rate"])
    assert r.exit_code == 0
    r = runner.invoke(app, ["alerts", "list", "--json"])
    assert json.loads(r.output) == []


def test_alerts_save_rejects_bad_rule(tmp_path: Path) -> None:
    r = runner.invoke(
        app, ["alerts", "save", "--rule-json", "{not json"]
    )
    assert r.exit_code == 2
    r = runner.invoke(
        app, ["alerts", "save", "--rule-json", '{"x": 1}']
    )
    assert r.exit_code == 2
    r = runner.invoke(
        app, ["alerts", "save", "--rule-json", "[1]"]
    )
    assert r.exit_code == 2


def test_alerts_delete_missing_rule(tmp_path: Path) -> None:
    r = runner.invoke(app, ["alerts", "delete", "ghost"])
    assert r.exit_code == 2
    assert "not found" in r.output


def test_alerts_evaluate_no_rules(
    seeded_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    r = runner.invoke(
        app, ["alerts", "evaluate", "--db", str(seeded_db), "--json"]
    )
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert out == {"fired": [], "evaluated": 0, "project_id": "default"}


# -- eval-suggest -------------------------------------------------------------


def test_eval_suggest_recurring_across_two_traces(seeded_db: Path) -> None:
    r = _invoke(
        ["eval-suggest", "a" * 32, "b" * 32, "--json"], seeded_db
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["suggestions"]
    # same failure category across 2 distinct traces → recurring
    recurring = [
        s for s in out["suggestions"] if s["evidence"].get("recurring")
    ]
    assert recurring, out
    assert any(
        s["name"] == "tool_success" for s in out["suggestions"]
    )


def test_eval_suggest_missing_trace_exits_2(seeded_db: Path) -> None:
    r = _invoke(["eval-suggest", "0" * 32], seeded_db)
    assert r.exit_code == 2
    assert "not found" in r.output


def test_eval_suggest_requires_a_trace_id(seeded_db: Path) -> None:
    r = _invoke(["eval-suggest"], seeded_db)
    assert r.exit_code == 2


# -- intel --------------------------------------------------------------------


def test_intel_efficiency(seeded_db: Path) -> None:
    r = _invoke(["intel", "efficiency", "--json"], seeded_db)
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["traces"] == 3
    assert out["succeeded"] == 1
    assert out["success_rate"] == pytest.approx(1 / 3)


def test_intel_efficiency_tool_filter(seeded_db: Path) -> None:
    r = _invoke(
        ["intel", "efficiency", "--tool", "shell", "--json"], seeded_db
    )
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert out["traces"] == 3
    r = _invoke(
        ["intel", "efficiency", "--tool", "no-such-tool", "--json"],
        seeded_db,
    )
    assert json.loads(r.output)["traces"] == 0


def test_intel_correlate(seeded_db: Path) -> None:
    r = _invoke(["intel", "correlate", "--json"], seeded_db)
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["window"]["traces"] == 3
    assert "tables" in out


def test_intel_regress(seeded_db: Path) -> None:
    r = _invoke(
        [
            "intel", "regress",
            "--a-json", json.dumps(["a" * 32]),
            "--b-json", json.dumps(["c" * 32]),
            "--json",
        ],
        seeded_db,
    )
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["base_traces"] == 1
    assert out["candidate_traces"] == 1
    assert "success_rate" in out["deltas"]


def test_intel_regress_missing_and_bad_ids(seeded_db: Path) -> None:
    # missing trace ids are tolerated + reported (API parity), not fatal
    r = _invoke(
        [
            "intel", "regress",
            "--a-json", json.dumps(["0" * 32, "a" * 32]),
            "--b-json", json.dumps(["c" * 32]),
            "--json",
        ],
        seeded_db,
    )
    assert r.exit_code == 0
    out = json.loads(r.output)
    assert out["missing_trace_ids"] == {"a": ["0" * 32], "b": []}

    for opt, val in (
        ("--a-json", "{bad"),
        ("--a-json", "[]"),
        ("--a-json", json.dumps([1])),
        ("--b-json", json.dumps(["x"] * 501)),
    ):
        r = _invoke(
            [
                "intel", "regress",
                "--a-json", json.dumps(["a" * 32]),
                "--b-json", json.dumps(["c" * 32]),
                opt, val,
            ],
            seeded_db,
        )
        assert r.exit_code == 2, (opt, val)


# -- eval --evaluator/--params-json --------------------------------------------


def test_eval_evaluator_and_params(seeded_db: Path) -> None:
    r = _invoke(
        [
            "eval", "a" * 32,
            "--evaluator", "has_span",
            "--params-json", '{"name": "tool shell"}',
            "--json",
        ],
        seeded_db,
    )
    assert r.exit_code == 0, r.output
    rows = json.loads(r.output)
    names = [row["evaluator"] for row in rows]
    # defaults preserved + the requested extra
    assert names[:2] == ["error_free", "tool_success"]
    assert "has_span" in names
    hs = next(row for row in rows if row["evaluator"] == "has_span")
    assert hs["score"] == 1


def test_eval_repeatable_evaluator(seeded_db: Path) -> None:
    r = _invoke(
        [
            "eval", "a" * 32,
            "--evaluator", "tool_call_count",
            "--evaluator", "error_free",
            "--json",
        ],
        seeded_db,
    )
    assert r.exit_code == 0
    names = [row["evaluator"] for row in json.loads(r.output)]
    assert names.count("tool_call_count") == 1


def test_eval_invalid_evaluator_lists_registry(seeded_db: Path) -> None:
    from forge_lens.eval import EVALUATORS

    r = _invoke(["eval", "a" * 32, "--evaluator", "bogus"], seeded_db)
    assert r.exit_code == 2
    assert "unknown evaluator" in r.output
    assert "error_free" in r.output
    assert sorted(EVALUATORS)[0] in r.output


def test_eval_params_json_must_be_object(seeded_db: Path) -> None:
    r = _invoke(
        ["eval", "a" * 32, "--params-json", "[1]"], seeded_db
    )
    assert r.exit_code == 2
    r = _invoke(
        ["eval", "a" * 32, "--params-json", "{bad"], seeded_db
    )
    assert r.exit_code == 2


def test_eval_defaults_preserved(seeded_db: Path) -> None:
    r = _invoke(["eval", "a" * 32, "--json"], seeded_db)
    assert r.exit_code == 0
    names = [row["evaluator"] for row in json.loads(r.output)]
    assert names == ["error_free", "tool_success"]
