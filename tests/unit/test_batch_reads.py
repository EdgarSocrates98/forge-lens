"""Batch trace reads + storage write-path invariants (F1/F2/F4/F5).

- ``SQLiteTraceStore.get_traces`` hydrates identically to ``get_trace``
  (property-style check across a seeded store) while replacing the
  cohort/window endpoints' N+1 reads with a constant query count.
- ``upsert_trace`` prefetches stored span rows once per trace instead of
  once per span — dedup/merge semantics are unchanged, including an
  intra-trace span_id re-delivery that must still merge, not crash.
- List routes reject ``limit<1`` / ``offset<0`` with 422 instead of a
  silent clamp.
- ``install``/``uninstall`` receipt writes surface an unwritable
  data_dir as a clean ``{"ok": False}`` — never a traceback.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from forge_lens.api.app import create_app
from forge_lens.cli.main import app as cli_app
from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.core.config import LensConfig
from forge_lens.install import lifecycle
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _span(
    trace_id: str,
    n: int,
    *,
    parent: str | None = None,
    error: bool = False,
    attrs: dict[str, Any] | None = None,
) -> Span:
    code = StatusCode.ERROR if error else StatusCode.OK
    return Span(
        trace_id=trace_id,
        span_id=f"{n:016x}",
        parent_span_id=parent,
        name=f"op-{n}",
        kind=SpanKind.AGENT,
        start_time=_T0 + timedelta(milliseconds=10 * n),
        end_time=_T0 + timedelta(milliseconds=10 * n + 5),
        attributes=attrs or {},
        status=Status(code=code),
    )


def _trace(
    hex_id: str,
    *,
    project_id: str = "default",
    session_id: str | None = None,
    n_spans: int = 2,
    error: bool = False,
    minutes_late: int = 0,
) -> Trace:
    """A small deterministic trace; ``hex_id`` is a 0-255 int → trace_id."""
    tid = f"{hex_id:032x}"
    spans = [
        _span(tid, i + 1, parent=(f"{i:016x}" if i else None), error=error)
        for i in range(n_spans)
    ]
    started = _T0 + timedelta(minutes=minutes_late)
    return Trace(
        trace_id=tid,
        project_id=project_id,
        session_id=session_id,
        root_span_id=spans[0].span_id if spans else None,
        started_at=started,
        ended_at=started + timedelta(milliseconds=50),
        status=Status(code=StatusCode.ERROR if error else StatusCode.OK),
        spans=spans,
    )


@pytest.fixture
def seeded(tmp_path: Path) -> Iterator[SQLiteTraceStore]:
    store = SQLiteTraceStore(tmp_path / "lens.db")
    store.upsert_trace(_trace(0xA1, n_spans=3, session_id="s1"))
    store.upsert_trace(_trace(0xA2, n_spans=1, session_id="s1", error=True))
    store.upsert_trace(_trace(0xA3, n_spans=2, project_id="other"))
    yield store
    store.close()


# -- get_traces: parity + edge semantics -------------------------------------


def test_get_traces_hydration_matches_get_trace(seeded: SQLiteTraceStore) -> None:
    """Batch read must return byte-for-byte identical hydration."""
    ids = [f"{h:032x}" for h in (0xA1, 0xA2, 0xA3)]
    got = seeded.get_traces("default", ids[:2])
    got_other = seeded.get_traces("other", [ids[2]])
    for tid, t in {**got, **got_other}.items():
        single = seeded.get_trace(
            tid, project_id=t.project_id if t else "default"
        )
        if t is None or single is None:
            assert t is None
            assert single is None
            continue
        assert t.model_dump(mode="json") == single.model_dump(mode="json")


def test_get_traces_missing_and_edge_ids(seeded: SQLiteTraceStore) -> None:
    """Absent ids map to None — never fabricated; unencodable ids are
    'absent' exactly like get_trace's lone-surrogate rule."""
    ok_id = f"{0xA1:032x}"
    missing = "f" * 32
    got = seeded.get_traces("default", [ok_id, missing, "0" * 32])
    assert got[ok_id] is not None
    assert got[missing] is None
    assert got["0" * 32] is None
    # lone-surrogate input: same "absent" verdict get_trace gives
    assert seeded.get_traces("default", ["sur\ud800rogate"]) == {
        "sur\ud800rogate": None
    }
    assert seeded.get_trace("sur\ud800rogate", project_id="default") is None
    assert seeded.get_traces("default", []) == {}
    # wrong project → absent, same as get_trace
    assert seeded.get_traces("other", [ok_id])[ok_id] is None


def test_get_traces_wildcard_latest_last_seen(tmp_path: Path) -> None:
    """project_id=None mirrors get_trace's 'latest last_seen_ns wins'."""
    store = SQLiteTraceStore(tmp_path / "wild.db")
    tid = "b" * 32
    older = _trace(0xB, project_id="p1")
    newer = _trace(0xB, project_id="p2", minutes_late=60)
    older.trace_id = newer.trace_id = tid
    older.spans = [
        s.model_copy(update={"trace_id": tid}) for s in older.spans
    ]
    newer.spans = [
        s.model_copy(update={"trace_id": tid}) for s in newer.spans
    ]
    store.upsert_trace(older)
    store.upsert_trace(newer)
    single = store.get_trace(tid)
    batch = store.get_traces(None, [tid])[tid]
    assert single is not None
    assert batch is not None
    assert single.project_id == batch.project_id == "p2"
    assert batch.model_dump(mode="json") == single.model_dump(mode="json")
    store.close()


# -- query-count discipline ----------------------------------------------------


class _CountingConn:
    """sqlite3.Connection proxy counting SELECT round-trips."""

    _OWN_ATTRS = frozenset({"_conn", "selects"})

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "selects", 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_conn"), name, value)

    def execute(self, sql: str, parameters: object = ()) -> sqlite3.Cursor:  # type: ignore[override]
        if sql.lstrip().upper().startswith("SELECT"):
            object.__setattr__(
                self, "selects", object.__getattribute__(self, "selects") + 1
            )
        return object.__getattribute__(self, "_conn").execute(sql, parameters)

    def __enter__(self) -> _CountingConn:
        object.__getattribute__(self, "_conn").__enter__()
        return self

    def __exit__(self, *args: object) -> Any:
        return object.__getattribute__(self, "_conn").__exit__(*args)


def _count(store: SQLiteTraceStore) -> _CountingConn:
    counter = _CountingConn(store._conn)  # type: ignore[arg-type]
    store._conn = counter  # type: ignore[assignment]
    return counter


def test_get_traces_constant_query_count(tmp_path: Path) -> None:
    """500 ids → 2 SELECTs (traces + spans), not 1000."""
    store = SQLiteTraceStore(tmp_path / "q.db")
    for i in range(3):
        store.upsert_trace(_trace(i + 1))
    counter = _count(store)
    ids = [f"{i:032x}" for i in range(1, 4)]
    counter.selects = 0
    got = store.get_traces(
        "default", ids + [f"{i:032x}" for i in range(0x100, 0x100 + 497)]
    )
    assert counter.selects == 2  # one traces query + one spans query
    assert sum(t is not None for t in got.values()) == 3
    # chunk boundary: >500 ids → one extra chunk pair, still constant;
    # an all-miss chunk may skip its spans query (fewer, never more)
    counter.selects = 0
    got = store.get_traces(
        "default", ids + [f"{i:032x}" for i in range(0x200, 0x200 + 700)]
    )
    assert counter.selects <= 4
    assert sum(t is not None for t in got.values()) == 3
    store.close()


def test_upsert_trace_one_prefetch(tmp_path: Path) -> None:
    """Insert path: 1 span prefetch + 1 flag-refresh SELECT regardless
    of span count — no per-span PK lookup."""
    store = SQLiteTraceStore(tmp_path / "u.db")
    counter = _count(store)
    trace = _trace(0xC1, n_spans=10)
    counter.selects = 0
    assert store.upsert_trace(trace) == (10, 0, 0)
    assert counter.selects <= 3
    store.close()


def test_upsert_trace_dedup_semantics_unchanged(tmp_path: Path) -> None:
    """Re-delivery of the same trace → all duplicates, flags merged —
    identical to the pre-batch contract."""
    store = SQLiteTraceStore(tmp_path / "d.db")
    trace = _trace(0xD1, n_spans=4)
    assert store.upsert_trace(trace) == (4, 0, 0)
    assert store.upsert_trace(trace) == (0, 0, 4)
    stored = store.get_trace(trace.trace_id, project_id="default")
    assert stored is not None
    assert len(stored.spans) == 4
    # a changed span merges (updated), it does not double-write
    changed = trace.spans[1].model_copy(
        update={"status": Status(code=StatusCode.ERROR, message="late")}
    )
    redeliver = trace.model_copy(update={"spans": [trace.spans[0], changed]})
    assert store.upsert_trace(redeliver) == (0, 1, 1)
    stored = store.get_trace(trace.trace_id, project_id="default")
    assert stored is not None
    assert len(stored.spans) == 4
    store.close()


def test_upsert_trace_intra_trace_duplicate_span_id(tmp_path: Path) -> None:
    """Two occurrences of one span_id inside a single delivery: the
    second must merge/dup against the row the first just wrote — the
    prefetch can't see it, so the fallback re-read keeps old semantics."""
    store = SQLiteTraceStore(tmp_path / "dup.db")
    tid = "e" * 32
    first = _span(tid, 1)
    # identical re-delivery inside the same trace → duplicate, not crash
    same = _span(tid, 1)
    # changed content for a third occurrence → merged update
    changed = _span(tid, 1)
    changed.status = Status(code=StatusCode.ERROR, message="evolved")
    trace = Trace(
        trace_id=tid,
        started_at=_T0,
        ended_at=_T0 + timedelta(milliseconds=50),
        status=Status(code=StatusCode.OK),
        spans=[first, same, changed],
    )
    inserted, updated, dup = store.upsert_trace(trace)
    assert (inserted, updated, dup) == (1, 1, 1)
    stored = store.get_trace(tid, project_id="default")
    assert stored is not None
    assert len(stored.spans) == 1
    assert stored.spans[0].status.code is StatusCode.ERROR
    # the second occurrence still leaves DUPLICATE evidence on the row
    from forge_lens.contracts.v1.trace import IntegrityFlag

    assert IntegrityFlag.DUPLICATE in stored.spans[0].integrity_flags
    store.close()


# -- rewired endpoints exercise the batch path ---------------------------------


@pytest.fixture
def api(tmp_path: Path) -> Iterator[tuple[TestClient, SQLiteTraceStore]]:
    store = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db")
    with TestClient(create_app(store=store, config=cfg)) as c:
        yield c, store
    store.close()


def test_eval_run_404_on_first_missing(
    api: tuple[TestClient, SQLiteTraceStore],
) -> None:
    client, store = api
    store.upsert_trace(_trace(0x51))
    body = {
        "trace_ids": [f"{0x51:032x}", "f" * 32],
        "evaluators": [{"name": "error_free"}],
    }
    r = client.post("/v1/eval/run", json=body)
    assert r.status_code == 404
    assert r.json()["detail"] == "trace not found"
    body["trace_ids"] = [f"{0x51:032x}"]
    r = client.post("/v1/eval/run", json=body)
    assert r.status_code == 200, r.text


def test_regress_reports_missing_per_side(
    api: tuple[TestClient, SQLiteTraceStore],
) -> None:
    client, store = api
    store.upsert_trace(_trace(0x61))
    store.upsert_trace(_trace(0x62))
    r = client.post(
        "/v1/intel/regress",
        json={
            "a": {"trace_ids": [f"{0x61:032x}", "f" * 32]},
            "b": {"trace_ids": [f"{0x62:032x}"]},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["missing_trace_ids"] == {"a": ["f" * 32], "b": []}


def test_session_graph_uses_batch(
    api: tuple[TestClient, SQLiteTraceStore],
) -> None:
    client, store = api
    store.upsert_trace(_trace(0x71, session_id="sg"))
    store.upsert_trace(_trace(0x72, session_id="sg", minutes_late=5))
    r = client.get("/v1/graph/sessions/sg")
    assert r.status_code == 200, r.text
    assert r.json()["stats"]["trace_count"] == 2


def test_fleet_efficiency_missing_ids(
    api: tuple[TestClient, SQLiteTraceStore],
) -> None:
    client, store = api
    store.upsert_trace(_trace(0x81))
    r = client.post(
        "/v1/intel/efficiency",
        json={"trace_ids": [f"{0x81:032x}", "f" * 32]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["missing_trace_ids"] == ["f" * 32]


def test_experiment_run_404_on_missing(
    api: tuple[TestClient, SQLiteTraceStore],
) -> None:
    client, store = api
    store.upsert_trace(_trace(0x91))
    spec = {
        "name": "ab",
        "cohorts": {
            "a": {"trace_ids": [f"{0x91:032x}"]},
            "b": {"trace_ids": ["f" * 32]},
        },
        "evaluators": [{"name": "error_free"}],
    }
    assert client.put("/v1/experiments/e1", json=spec).status_code == 200
    r = client.post("/v1/experiments/e1/run", json={})
    assert r.status_code == 404


# -- F4: 422 on malformed pagination, consistently ----------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/traces",
        "/v1/sessions",
        "/v1/tools",
        "/v1/experiments",
    ],
)
def test_list_routes_reject_bad_pagination(
    api: tuple[TestClient, SQLiteTraceStore], path: str
) -> None:
    client, _ = api
    for params in ({"limit": -1}, {"limit": 0}, {"offset": -1}):
        r = client.get(path, params=params)
        assert r.status_code == 422, f"{path} {params} → {r.status_code}"
        assert isinstance(r.json()["detail"], str)
    # upper bound still clamps, never an error
    assert client.get(path, params={"limit": 9999}).status_code == 200


def test_experiment_detail_and_audit_and_session_graph_pagination(
    api: tuple[TestClient, SQLiteTraceStore],
) -> None:
    client, _ = api
    spec = {
        "cohorts": {
            "a": {"trace_ids": ["t1"]},
            "b": {"trace_ids": ["t2"]},
        },
        "evaluators": [{"name": "error_free"}],
    }
    assert client.put("/v1/experiments/e1", json=spec).status_code == 200
    r = client.get("/v1/experiments/e1", params={"limit": 0})
    assert r.status_code == 422
    r = client.get("/v1/experiments/e1", params={"offset": -2})
    assert r.status_code == 422
    assert client.get("/v1/admin/audit", params={"limit": 0}).status_code == 422
    assert client.get("/v1/admin/audit", params={"limit": 501}).status_code == 422
    assert client.get("/v1/admin/audit", params={"offset": -1}).status_code == 422
    assert (
        client.get("/v1/graph/sessions/x", params={"limit": 0}).status_code
        == 422
    )
    # sane pagination unchanged
    assert client.get("/v1/experiments/e1", params={"limit": 1}).status_code == 200
    assert client.get("/v1/admin/audit", params={"limit": 5}).status_code == 200


# -- F5: lifecycle receipt OSError → clean error, no traceback -----------------


def test_install_receipt_write_failure_is_clean(tmp_path: Path) -> None:
    """data_dir occupied by a FILE → receipt write can't happen;
    install() reports it, it does not raise."""
    blocker = tmp_path / "data-dir"
    blocker.write_text("not a directory")
    out = lifecycle.install(
        blocker, profile="recommended", source="local",
        files=[], ts_ns=1,
    )
    assert out["ok"] is False
    assert "receipt write failed" in out["error"]

    out = lifecycle.uninstall(blocker, ts_ns=1)
    assert out["ok"] is False
    assert "receipt write failed" in out["error"]


def test_cli_install_uninstall_unwritable_dir_no_traceback(tmp_path: Path) -> None:
    runner = CliRunner()
    blocker = tmp_path / "data-dir"
    blocker.write_text("not a directory")
    r = runner.invoke(cli_app, ["install", "--data-dir", str(blocker)])
    assert r.exit_code == 1
    assert "Traceback" not in r.output
    assert "receipt write failed" in r.output

    r = runner.invoke(cli_app, ["install", "uninstall", "--data-dir", str(blocker)])
    assert r.exit_code == 1
    assert "Traceback" not in r.output
    assert "receipt write failed" in r.output


def test_install_uninstall_happy_path_still_ok(tmp_path: Path) -> None:
    """Backward compat: success returns keep their fields; uninstall
    now also reports ``ok``/``failed`` for partial-removal honesty."""
    d = tmp_path / "d"
    out = lifecycle.install(
        d, profile="recommended", source="local",
        files=["managed/x.txt"], ts_ns=1,
    )
    assert out["ok"] is True
    managed = d / "managed" / "x.txt"
    managed.parent.mkdir(parents=True, exist_ok=True)
    managed.write_text("payload")
    out = lifecycle.uninstall(d, ts_ns=2)
    assert out["ok"] is True
    assert out["removed"] == ["managed/x.txt"]
    assert out["prior_receipt"]["action"] == "install"
    assert not managed.exists()
