"""Cycle 13 — persisted experiments (audit gap 06.3).

``/v1/experiments`` pins an A/B eval spec (cohorts + evaluators) so
compare runs stop being ephemeral; ``/v1/experiments/{id}/run`` executes
the same compare path as ``/v1/eval/compare`` and appends the report to
``experiment_results`` — append-only, tenancy-scoped like datasets.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from forge_lens.api.app import create_app
from forge_lens.contracts.v1.experiment import Experiment, ExperimentCohort
from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.core.config import LensConfig
from forge_lens.core.errors import ValidationError
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _trace(trace_id: str, *, project_id: str = "default",
           error: bool = False) -> Trace:
    code = StatusCode.ERROR if error else StatusCode.OK
    span = Span(
        trace_id=trace_id,
        span_id="1" * 16,
        name="op",
        kind=SpanKind.AGENT,
        start_time=_T0,
        end_time=_T0 + timedelta(milliseconds=100),
        status=Status(code=code),
    )
    return Trace(
        trace_id=trace_id,
        project_id=project_id,
        started_at=_T0,
        ended_at=_T0 + timedelta(milliseconds=100),
        status=Status(code=code),
        spans=[span],
    )


def _spec(a_ids: list[str], b_ids: list[str], **kw: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": "ab experiment",
        "description": "baseline vs candidate",
        "dataset_id": "qa",
        "dataset_version": "v1",
        "cohorts": {
            "a": {"trace_ids": a_ids, "label": "baseline",
                  "config": {"model": "m1", "prompt": "p1"}},
            "b": {"trace_ids": b_ids, "label": "candidate",
                  "config": {"model": "m2"}},
        },
        "evaluators": [{"name": "error_free"}],
        "metadata": {"owner": "evals"},
    }
    body.update(kw)
    return body


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteTraceStore]:
    s = SQLiteTraceStore(tmp_path / "exp.db")
    yield s
    s.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    s = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db")
    app = create_app(store=s, config=cfg)
    with TestClient(app) as c:
        yield c
    s.close()


# -- contract ----------------------------------------------------------


def test_experiment_contract_shape() -> None:
    exp = Experiment(experiment_id="e1")
    assert exp.schema_version == "1.0.0"
    assert exp.project_id == "default"
    assert exp.status == "open"
    assert exp.result_ref is None
    assert exp.provenance.value == "imported"
    assert exp.cohorts.a.trace_ids == []
    c = ExperimentCohort(trace_ids=["t1"], label="a", config={"model": "m"})
    assert c.schema_version == "1.0.0"
    dumped = Experiment(
        experiment_id="e1", cohorts={"a": c, "b": {"trace_ids": ["t2"]}}
    ).model_dump(mode="json")
    assert dumped["cohorts"]["b"]["trace_ids"] == ["t2"]


# -- store --------------------------------------------------------------


def test_experiment_store_roundtrip(store: SQLiteTraceStore) -> None:
    store.upsert_experiment(
        "e1",
        project_id="p1",
        name="exp",
        cohorts={"a": {"trace_ids": ["t1"]}, "b": {"trace_ids": ["t2"]}},
        evaluators=[{"name": "error_free"}],
        metadata={"k": "v"},
        now_ns=1_000,
    )
    exp = store.get_experiment("e1", "p1")
    assert exp is not None
    assert exp["status"] == "open"
    assert exp["result_ref"] is None
    assert exp["cohorts"]["a"]["trace_ids"] == ["t1"]
    assert exp["provenance"] == "imported"
    # tenant isolation at the store boundary
    assert store.get_experiment("e1", "other") is None
    assert [e["experiment_id"] for e in store.list_experiments("p1")] == ["e1"]
    assert store.list_experiments("other") == []

    rid1 = store.record_experiment_result(
        "e1", project_id="p1", report={"deltas": []}, run_ns=2_000
    )
    rid2 = store.record_experiment_result(
        "e1", project_id="p1", report={"deltas": []}, run_ns=3_000
    )
    assert rid1 != rid2
    runs = store.list_experiment_results("e1", "p1")
    assert [r["run_id"] for r in runs] == [rid1, rid2]  # append order
    exp = store.get_experiment("e1", "p1")
    assert exp is not None
    assert exp["status"] == "completed"
    assert exp["result_ref"] == f"run:{rid2}"
    # explicit lifecycle move — reopen keeps run history intact
    assert store.set_experiment_status("e1", "open", project_id="p1") is True
    assert store.set_experiment_status("e9", "open", project_id="p1") is False
    assert len(store.list_experiment_results("e1", "p1")) == 2
    # results for a nonexistent experiment never write silently
    with pytest.raises(ValidationError):
        store.record_experiment_result(
            "ghost", project_id="p1", report={}, run_ns=4_000
        )


# -- API: CRUD ------------------------------------------------------------


def test_experiments_create_list_get_404(client: TestClient) -> None:
    r = client.put("/v1/experiments/e1", json=_spec(["a" * 32], ["b" * 32]))
    assert r.status_code == 200, r.text
    assert r.json()["stored"] is True

    r = client.get("/v1/experiments")
    assert r.status_code == 200
    env = r.json()
    assert env["limit"] == 100
    assert env["offset"] == 0
    assert [i["experiment_id"] for i in env["items"]] == ["e1"]
    assert env["items"][0]["status"] == "open"

    r = client.get("/v1/experiments/e1")
    assert r.status_code == 200
    exp = r.json()
    assert exp["name"] == "ab experiment"
    assert exp["dataset_id"] == "qa"
    assert exp["cohorts"]["a"]["config"]["model"] == "m1"
    assert exp["evaluators"] == [{"name": "error_free"}]
    assert exp["runs"] == []

    assert client.get("/v1/experiments/nope").status_code == 404
    # list envelope clamps like every other collection
    r = client.get("/v1/experiments", params={"limit": 9_999})
    assert r.json()["limit"] == 500


def test_experiments_put_validation_422(client: TestClient) -> None:
    assert client.put("/v1/experiments/e1", json={}).status_code == 422
    assert client.put("/v1/experiments/e1", json={
        "evaluators": [{"name": "error_free"}],
    }).status_code == 422  # cohorts required
    assert client.put("/v1/experiments/e1", json={
        "cohorts": {"a": {"trace_ids": ["t"]}},  # missing b
        "evaluators": [{"name": "error_free"}],
    }).status_code == 422
    assert client.put("/v1/experiments/e1", json={
        "cohorts": {"a": {"trace_ids": []}, "b": {"trace_ids": ["t"]}},
        "evaluators": [{"name": "error_free"}],
    }).status_code == 422  # empty side
    assert client.put("/v1/experiments/e1", json={
        "cohorts": {"a": {"trace_ids": ["t"]}, "b": {"trace_ids": ["t"]}},
    }).status_code == 422  # evaluators required
    assert client.put("/v1/experiments/e1", json=_spec(
        ["t"], ["t"], dataset_id=None, dataset_version="v9",
    )).status_code == 422  # version without dataset
    assert client.put("/v1/experiments/e1", json=_spec(
        ["t"], ["t"], status="archived",
    )).status_code == 422  # honest lifecycle only
    assert client.put("/v1/experiments/e1", json=_spec(
        ["t"], ["t"], metadata="nope",
    )).status_code == 422


def test_experiments_non_finite_spec_rejected(client: TestClient) -> None:
    """NaN/Infinity literals parse via json.loads — a declared spec must
    reject them at the boundary, never silently null-coerce."""
    for bad in ("NaN", "Infinity", "-Infinity"):
        r = client.put("/v1/experiments/e-nf", content=(
            '{"name": "x", "cohorts": {'
            '"a": {"trace_ids": ["t1"], "config": {"temperature": '
            + bad + '}},'
            '"b": {"trace_ids": ["t2"]}},'
            '"evaluators": [{"name": "error_free"}]}'),
            headers={"content-type": "application/json"})
        assert r.status_code == 422, (bad, r.status_code, r.text)
    r = client.put("/v1/experiments/e-nf2", content=(
        '{"name": "x", "metadata": {"threshold": Infinity},'
        '"cohorts": {"a": {"trace_ids": ["t1"]},'
        '"b": {"trace_ids": ["t2"]}},'
        '"evaluators": [{"name": "error_free"}]}'),
        headers={"content-type": "application/json"})
    assert r.status_code == 422
    # control: a finite spec still stores
    assert client.put(
        "/v1/experiments/e-ok", json=_spec(["t1"], ["t2"])
    ).status_code == 200


# -- API: run -------------------------------------------------------------


def _seed_and_spec(store: SQLiteTraceStore) -> dict[str, Any]:
    ok = _trace("a" * 32)
    bad = _trace("b" * 32, error=True)
    store.upsert_trace(ok)
    store.upsert_trace(bad)
    return _spec([ok.trace_id], [bad.trace_id])


def test_experiment_run_and_append_only_runs(tmp_path: Path) -> None:
    store = SQLiteTraceStore(tmp_path / "run.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "run.db")
    app = create_app(store=store, config=cfg)
    try:
        with TestClient(app) as c:
            spec = _seed_and_spec(store)
            assert c.put("/v1/experiments/e1", json=spec).status_code == 200

            r = c.post("/v1/experiments/e1/run", json={})
            assert r.status_code == 200, r.text
            out = r.json()
            assert out["run_id"] >= 1
            assert out["run_at"] is not None
            report = out["report"]
            assert report["a"]["trace_ids"] == ["a" * 32]
            assert report["b"]["trace_ids"] == ["b" * 32]
            assert report["a"]["dataset"] == "qa@v1"
            keys = {f"{d['evaluator']}/{d['metric']}" for d in report["deltas"]}
            assert "error_free/no_error_spans" in keys

            exp = c.get("/v1/experiments/e1").json()
            assert exp["status"] == "completed"
            assert exp["result_ref"] == f"run:{out['run_id']}"
            assert len(exp["runs"]) == 1
            assert exp["runs"][0]["report"] == report

            # append-only: a second run is a second row, history intact
            r2 = c.post("/v1/experiments/e1/run", json={})
            assert r2.status_code == 200
            exp = c.get("/v1/experiments/e1").json()
            assert [r_["run_id"] for r_ in exp["runs"]] == sorted(
                [out["run_id"], r2.json()["run_id"]]
            )
            assert len(exp["runs"]) == 2
            assert exp["runs"][0]["run_id"] == out["run_id"]

            # missing experiment → 404, never a fabricated run
            assert c.post("/v1/experiments/ghost/run", json={}
                          ).status_code == 404
            # cohort evidence pruned → the run fails loudly (404), not
            # a partial comparison presented as complete
            assert c.put("/v1/experiments/e2", json=_spec(
                ["f" * 16], ["b" * 32])).status_code == 200
            assert c.post("/v1/experiments/e2/run", json={}
                          ).status_code == 404

            # optional gate — same contract as /v1/eval/compare
            r = c.post("/v1/experiments/e1/run", json={
                "gate": {"metrics": {
                    "error_free/no_error_spans": {"min_pass_rate_b": 1.0}}}}
            )
            assert r.status_code == 200
            assert r.json()["report"]["gate"]["gate_passed"] is False
            assert c.post("/v1/experiments/e1/run", json={
                "gate": "x"}).status_code == 422
    finally:
        store.close()


# -- tenancy ---------------------------------------------------------------


def test_experiment_tenancy_keys_mode(tmp_path: Path) -> None:
    """Scoped keys: p2 can never touch p1 experiments — 403 on foreign
    scope, 404 on same-id-different-tenant; reader can't run (writer)."""
    from forge_lens.security.keys import generate_key

    store = SQLiteTraceStore(tmp_path / "k.db")
    store.upsert_trace(_trace("a" * 32, project_id="p1"))
    store.upsert_trace(_trace("b" * 32, project_id="p1", error=True))
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "k.db", auth_mode="keys"
    )
    app = create_app(store=store, config=cfg)
    _, secret, h = generate_key()
    store.create_api_key("root", name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": secret}
            spec = _spec(["a" * 32], ["b" * 32], project_id="p1")
            assert c.put("/v1/experiments/e1", json=spec,
                         headers=admin).status_code == 200
            assert c.post("/v1/experiments/e1/run", json={"project_id": "p1"},
                          headers=admin).status_code == 200

            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "w1", "role": "writer",
                             "projects": ["p1"]})
            w1 = {"X-Forge-Lens-Key": r.json()["secret"]}
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "w2", "role": "writer",
                             "projects": ["p2"]})
            w2 = {"X-Forge-Lens-Key": r.json()["secret"]}
            r = c.post("/v1/admin/keys", headers=admin,
                       json={"name": "r1", "role": "reader",
                             "projects": ["p1"]})
            r1 = {"X-Forge-Lens-Key": r.json()["secret"]}

            # in-scope writer: full lifecycle works
            assert c.get("/v1/experiments/e1?project_id=p1",
                         headers=w1).status_code == 200
            assert c.post("/v1/experiments/e1/run",
                          json={"project_id": "p1"},
                          headers=w1).status_code == 200

            # foreign tenant: every surface 403s — never leaks, never writes
            for r in (
                c.get("/v1/experiments/e1?project_id=p1", headers=w2),
                c.get("/v1/experiments?project_id=p1", headers=w2),
                c.put("/v1/experiments/e1", json=spec, headers=w2),
                c.post("/v1/experiments/e1/run",
                       json={"project_id": "p1"}, headers=w2),
            ):
                assert r.status_code == 403, r.text
            # omitted project_id resolves cfg.project_id → outside p2 scope
            assert c.get("/v1/experiments/e1", headers=w2).status_code == 403
            # same id in the caller's own tenant is a different
            # experiment → honest 404, not p1's data
            assert c.get("/v1/experiments/e1?project_id=p2",
                         headers=w2).status_code == 404
            # reader can read but cannot write or run
            assert c.get("/v1/experiments/e1?project_id=p1",
                         headers=r1).status_code == 200
            assert c.post("/v1/experiments/e1/run",
                          json={"project_id": "p1"},
                          headers=r1).status_code == 403
            assert c.put("/v1/experiments/e9", json=spec,
                         headers=r1).status_code == 403
            # unauthenticated: reads AND writes denied under keys mode
            assert c.get("/v1/experiments").status_code == 401
            assert c.put("/v1/experiments/e9", json=spec).status_code == 401
            # p1's run history is untouched by foreign attempts
            exp = c.get("/v1/experiments/e1?project_id=p1",
                        headers=admin).json()
            assert len(exp["runs"]) == 2
    finally:
        store.close()
