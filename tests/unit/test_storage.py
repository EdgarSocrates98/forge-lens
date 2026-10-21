from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from unittest.mock import patch

import forge_lens.storage.sqlite as store_mod
from forge_lens.contracts.v1.trace import IntegrityFlag, StatusCode, Trace
from forge_lens.storage.sqlite import SQLiteTraceStore


class _SpyConn:
    """Connection proxy: records in_transaction during DML, can fail mid-write."""

    _OWN_ATTRS = frozenset({"_c", "in_tx_during_dml", "fail_after_dml"})

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_c", conn)
        object.__setattr__(self, "in_tx_during_dml", [])
        object.__setattr__(self, "fail_after_dml", None)

    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        return getattr(object.__getattribute__(self, "_c"), name)

    def __setattr__(self, name: str, value: object) -> None:
        if name in self._OWN_ATTRS:
            object.__setattr__(self, name, value)
        else:
            setattr(object.__getattribute__(self, "_c"), name, value)

    def execute(self, sql: str, parameters: object = ()):  # type: ignore[no-untyped-def]
        conn = object.__getattribute__(self, "_c")
        result = conn.execute(sql, parameters)
        if sql.lstrip().upper()[:6] in ("INSERT", "UPDATE"):
            in_tx = object.__getattribute__(self, "in_tx_during_dml")
            # post-execute: True iff the write sits inside an open transaction
            # (autocommit mode would already have committed it → False)
            in_tx.append(conn.in_transaction)
            fail_at = object.__getattribute__(self, "fail_after_dml")
            if fail_at is not None and len(in_tx) == fail_at:
                raise sqlite3.OperationalError("simulated crash")
        return result

    def __enter__(self) -> _SpyConn:
        return self

    def __exit__(self, *args: object) -> bool:
        return object.__getattribute__(self, "_c").__exit__(*args)  # type: ignore[return-value]


def _spy_connect() -> list[_SpyConn]:
    spies: list[_SpyConn] = []
    real = store_mod.sqlite3.connect

    def factory(*args: object, **kwargs: object) -> _SpyConn:
        spy = _SpyConn(real(*args, **kwargs))  # type: ignore[arg-type]
        spies.append(spy)
        return spy

    patcher = patch.object(store_mod.sqlite3, "connect", side_effect=factory)
    patcher.start()
    return spies


def test_upsert_and_get_roundtrip(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    inserted, updated, dup = tmp_store.upsert_trace(synthetic_trace)
    assert (inserted, updated, dup) == (3, 0, 0)

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    assert loaded.trace_id == synthetic_trace.trace_id
    assert len(loaded.spans) == 3
    llm = next(s for s in loaded.spans if s.kind.value == "llm")
    assert llm.attributes["forge_lens.token_usage"]["input_tokens"] == 100
    assert loaded.status.code == StatusCode.ERROR  # tool span failed


def test_reingest_is_idempotent(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    tmp_store.upsert_trace(synthetic_trace)
    inserted, updated, dup = tmp_store.upsert_trace(synthetic_trace)
    assert (inserted, updated, dup) == (0, 0, 3)
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert len(loaded.spans) == 3  # still 3, no duplication


def test_duplicate_flag_records_redelivery(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    tmp_store.upsert_trace(synthetic_trace)
    tmp_store.upsert_trace(synthetic_trace)
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    for span in loaded.spans:
        assert IntegrityFlag.DUPLICATE in span.integrity_flags


def test_partial_flush_merges(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    first = synthetic_trace.model_copy(deep=True)
    first.spans = synthetic_trace.spans[:1]
    tmp_store.upsert_trace(first)
    second = synthetic_trace.model_copy(deep=True)
    second.spans = synthetic_trace.spans[1:]
    tmp_store.upsert_trace(second)
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert len(loaded.spans) == 3


def test_late_end_time_merges_not_duplicates(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Regression: a running span must complete when its end arrives late."""
    running = synthetic_trace.model_copy(deep=True)
    running.spans = [running.spans[0]]
    running.spans[0].end_time = None
    tmp_store.upsert_trace(running)

    finishing = synthetic_trace.model_copy(deep=True)
    finishing.spans = [finishing.spans[0]]
    inserted, updated, dup = tmp_store.upsert_trace(finishing)

    assert (inserted, updated, dup) == (0, 1, 0)
    stored = tmp_store.get_trace(synthetic_trace.trace_id).spans[0]
    assert stored.end_time == synthetic_trace.spans[0].end_time
    assert IntegrityFlag.MISSING_END not in stored.integrity_flags
    # the stored digest now matches the *merged* content
    inserted, updated, dup = tmp_store.upsert_trace(finishing)
    assert (inserted, updated, dup) == (0, 0, 1)


def test_missing_parent_flag_clears_when_parent_arrives(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    child_first = synthetic_trace.model_copy(deep=True)
    child_first.spans = [child_first.spans[1]]  # llm span, parent absent
    child_first.spans[0].integrity_flags = [IntegrityFlag.MISSING_PARENT]
    tmp_store.upsert_trace(child_first)

    parent_later = synthetic_trace.model_copy(deep=True)
    parent_later.spans = [parent_later.spans[0], parent_later.spans[1]]
    tmp_store.upsert_trace(parent_later)

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    llm = next(s for s in loaded.spans if s.name == "llm gpt-test")
    assert IntegrityFlag.MISSING_PARENT not in llm.integrity_flags


def test_writes_run_inside_transaction(tmp_path, synthetic_trace: Trace) -> None:
    """Every DML in upsert_trace must execute inside a real transaction."""
    spies = _spy_connect()
    try:
        store = SQLiteTraceStore(tmp_path / "tx.db")
        store.upsert_trace(synthetic_trace)
        spy = spies[-1]
        assert spy.in_tx_during_dml, "expected DML calls to be observed"
        assert all(spy.in_tx_during_dml)
        store.close()
    finally:
        patch.stopall()


def test_upsert_is_atomic(tmp_path, synthetic_trace: Trace) -> None:
    """A failure after one span was inserted must roll the whole batch back."""
    spies = _spy_connect()
    try:
        store = SQLiteTraceStore(tmp_path / "atomic.db")
        spy = spies[-1]
        spy.fail_after_dml = 2  # crash on the 2nd span INSERT
        try:
            store.upsert_trace(synthetic_trace)
            raise AssertionError("expected crash")
        except sqlite3.OperationalError:
            pass
        assert store.get_trace(synthetic_trace.trace_id) is None
        assert store.stats()["spans"] == 0

        # recovery: the same redelivery now persists everything
        assert store.upsert_trace(synthetic_trace) == (3, 0, 0)
        assert len(store.get_trace(synthetic_trace.trace_id).spans) == 3  # type: ignore[union-attr]
        store.close()
    finally:
        patch.stopall()


def test_store_rejects_span_trace_id_mismatch(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """The store itself defends consistency: a span naming a different
    trace_id would be persisted under a key get_trace can never reach."""
    import pytest

    from forge_lens.core.errors import ValidationError

    bad = synthetic_trace.model_copy(deep=True)
    bad.spans[1].trace_id = "f" * 32
    with pytest.raises(ValidationError, match="trace_id mismatch"):
        tmp_store.upsert_trace(bad)
    # nothing persisted — the check runs before any write
    assert tmp_store.get_trace(synthetic_trace.trace_id) is None
    assert tmp_store.stats()["spans"] == 0


def test_epoch_zero_started_ns_is_not_treated_as_missing(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Unix epoch (0 ns) is real data — `x or fallback` truthiness would
    silently drop it; explicit `is not None` must be used instead."""
    epoch = synthetic_trace.model_copy(deep=True)
    epoch.started_at = datetime.fromtimestamp(0, tz=UTC)
    for s in epoch.spans:
        s.start_time = epoch.started_at
    tmp_store.upsert_trace(epoch)
    loaded = tmp_store.get_trace(epoch.trace_id)
    assert loaded is not None
    assert loaded.started_at.timestamp() == 0


def test_wall_clock_free(tmp_store: SQLiteTraceStore) -> None:
    """storage/ must not read the wall clock (AGENTS.md hard rule)."""
    import inspect

    import forge_lens.storage.sqlite as mod

    src = inspect.getsource(mod)
    assert "datetime.now" not in src
    assert "time.time()" not in src


def test_list_and_stats(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    tmp_store.upsert_trace(synthetic_trace)
    items = tmp_store.list_traces(project_id="default")
    assert len(items) == 1
    assert items[0]["trace_id"] == synthetic_trace.trace_id
    assert items[0]["error_count"] == 1
    stats = tmp_store.stats()
    assert stats["traces"] == 1
    assert stats["spans"] == 3
    items_err = tmp_store.list_traces(project_id="default", status="error")
    assert len(items_err) == 1
    items_ok = tmp_store.list_traces(project_id="default", status="ok")
    assert items_ok == []


def test_trace_status_recomputes_from_stored_spans(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Span status updates flow into the denormalized trace status."""
    # first delivery: the tool span is still running (status unset)
    running = synthetic_trace.model_copy(deep=True)
    running.spans[2].status.code = StatusCode.UNSET
    running.spans[2].status.message = None
    running.status.code = StatusCode.UNSET
    tmp_store.upsert_trace(running)
    loaded0 = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded0 is not None
    assert loaded0.status.code == StatusCode.UNSET

    # the tool span is re-delivered with its real outcome: error
    outcome = synthetic_trace.model_copy(deep=True)
    outcome.spans = [outcome.spans[2]]
    tmp_store.upsert_trace(outcome)
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    assert loaded.status.code == StatusCode.ERROR

    # severity merge is monotone: a later 'ok' cannot erase an observed error
    downgrade = synthetic_trace.model_copy(deep=True)
    downgrade.spans = [downgrade.spans[2]]
    downgrade.spans[0].status.code = StatusCode.OK
    tmp_store.upsert_trace(downgrade)
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    assert loaded.status.code == StatusCode.ERROR


def test_upsert_merges_trace_level_columns(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Re-ingest must not keep stale first-seen trace-level fields:
    status_message fills when provided, resource attrs merge,
    provenance keeps the strongest, a named source beats 'unknown'."""
    from forge_lens.contracts.provenance import Provenance
    from forge_lens.contracts.v1.trace import Status

    first = synthetic_trace.model_copy(deep=True)
    first.status = Status(code=StatusCode.ERROR)  # no message yet
    first.resource_attributes = {"service.name": "svc"}
    tmp_store.upsert_trace(first)

    second = synthetic_trace.model_copy(deep=True)
    second.status = Status(code=StatusCode.ERROR, message="upstream timeout")
    second.resource_attributes = {"service.name": "svc", "cloud.region": "us-1"}
    second.source.system = "real-producer"
    second.provenance = Provenance.INFERRED  # weaker — must not downgrade
    tmp_store.upsert_trace(second)

    stored = tmp_store.get_trace(synthetic_trace.trace_id)
    assert stored is not None
    # latest non-null status_message wins
    assert stored.status.message == "upstream timeout"
    # resource attributes merged key-wise (incoming keys overlay stored)
    assert stored.resource_attributes["cloud.region"] == "us-1"
    assert stored.resource_attributes["service.name"] == "svc"
    # identified source replaces 'unknown'; pytest source stays
    assert stored.source.system == "real-producer"
    # provenance keeps the strongest: stored 'observed' survives 'inferred'
    assert stored.provenance == Provenance.OBSERVED


def test_duplicate_merge_identical_content_still_records_flags(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """A re-delivery counted as duplicate must still record evidence:
    the DUPLICATE flag plus any incoming-only flags — on BOTH paths
    (identical hash, and content that merges back to identical)."""
    tmp_store.upsert_trace(synthetic_trace)

    # path 1: identical hash + an incoming-only flag
    again = synthetic_trace.model_copy(deep=True)
    again.spans[1].integrity_flags = [IntegrityFlag.TRUNCATED]
    tmp_store.upsert_trace(again)

    # path 2: different hash, merges to identical — a running view of a
    # completed span (end_time=None) re-delivered with another flag
    running_view = synthetic_trace.model_copy(deep=True)
    running_view.spans = [running_view.spans[2]]
    running_view.spans[0].end_time = None
    running_view.spans[0].integrity_flags = [IntegrityFlag.MISSING_END]
    tmp_store.upsert_trace(running_view)

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    flags1 = loaded.spans[1].integrity_flags
    assert IntegrityFlag.TRUNCATED in flags1
    assert IntegrityFlag.DUPLICATE in flags1
    flags2 = loaded.spans[2].integrity_flags
    assert IntegrityFlag.DUPLICATE in flags2
    # MISSING_END from the running view was unioned then correctly
    # cleared by the refresh pass — stored data has an end_time
    assert IntegrityFlag.MISSING_END not in flags2


def test_conflicting_redelivery_flags_conflict_not_duplicate(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Same (project, trace, span) key carrying a different name/kind/
    start is a CONFLICT — the stored row was not replayed verbatim, so
    it must not be labeled DUPLICATE."""
    tmp_store.upsert_trace(synthetic_trace)

    lie = synthetic_trace.model_copy(deep=True)
    lie.spans = [lie.spans[0]]
    lie.spans[0].name = "completely.different.name"
    tmp_store.upsert_trace(lie)

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    flags = loaded.spans[0].integrity_flags
    assert IntegrityFlag.CONFLICT in flags
    assert IntegrityFlag.DUPLICATE not in flags
    # first-observed identity wins — the conflicting name was NOT applied
    assert loaded.spans[0].name == synthetic_trace.spans[0].name


def test_conflicting_update_merge_marks_conflict(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """A re-delivery that both merges new data AND conflicts on identity
    still records the conflict on the updated row."""
    tmp_store.upsert_trace(synthetic_trace)

    conflicted = synthetic_trace.model_copy(deep=True)
    conflicted.spans = [conflicted.spans[1]]
    conflicted.spans[0].name = "renamed.span"          # identity conflict
    conflicted.spans[0].attributes = {"extra": "new"}  # mergeable change
    tmp_store.upsert_trace(conflicted)

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    span = loaded.spans[1]
    assert IntegrityFlag.CONFLICT in span.integrity_flags
    assert span.name == synthetic_trace.spans[1].name  # identity kept
    assert span.attributes["extra"] == "new"           # merge still applied


def test_db_size_includes_wal_sidecars(
    tmp_path, tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """db_size_bytes must count -wal/-shm sidecars, not just the main
    file — WAL mode holds most live data there between checkpoints."""
    tmp_store.upsert_trace(synthetic_trace)
    wal = tmp_path / "lens.db-wal"
    assert wal.exists()  # uncheckpointed write stays in the wal
    assert wal.stat().st_size > 0
    expected = sum(
        f.stat().st_size for f in tmp_path.iterdir() if f.name.startswith("lens.db")
    )
    assert tmp_store.stats()["db_size_bytes"] == expected
    # the wal dominates here — main file alone would undercount badly
    assert expected > (tmp_path / "lens.db").stat().st_size
