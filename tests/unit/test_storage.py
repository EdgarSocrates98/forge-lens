from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import forge_lens.storage.sqlite as store_mod
from forge_lens.contracts.v1.trace import IntegrityFlag, Status, StatusCode, Trace
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
    """The deterministic core must not read the wall clock (AGENTS.md
    hard rule). Interval timers (monotonic/perf_counter — injectable in
    ingest/) are fine; datetime.now/time.time are not."""
    import inspect

    import forge_lens.api.app
    import forge_lens.ingest.pipeline
    import forge_lens.otlp.normalize
    import forge_lens.sanitize.redact
    import forge_lens.storage.sqlite

    for mod in (
        forge_lens.storage.sqlite,
        forge_lens.ingest.pipeline,
        forge_lens.api.app,
        forge_lens.otlp.normalize,
        forge_lens.sanitize.redact,
    ):
        src = inspect.getsource(mod)
        assert "datetime.now" not in src, mod.__name__
        assert "time.time()" not in src, mod.__name__


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


def test_partial_trace_completes_ended_at_filled(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """SQLite multi-arg MAX(x, NULL) → NULL — a stored partial trace
    (ended_ns NULL) must still gain ended_at when the completed
    redelivery arrives (the mid-flight-flush → root-end flow)."""
    partial = synthetic_trace.model_copy(deep=True)
    partial.ended_at = None
    partial.spans = [partial.spans[0].model_copy(update={"end_time": None})]
    tmp_store.upsert_trace(partial)
    assert tmp_store.get_trace(synthetic_trace.trace_id).ended_at is None  # type: ignore[union-attr]

    tmp_store.upsert_trace(synthetic_trace)  # completed re-delivery
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    assert loaded.ended_at == synthetic_trace.ended_at
    item = tmp_store.list_traces()[0]
    assert item["ended_at"] is not None


def test_spanless_trace_status_not_clobbered(
    tmp_store: SQLiteTraceStore,
) -> None:
    """A spanless trace's declared status must survive re-delivery —
    the rollup has no spans to derive from and must not reset to unset."""
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    declared = Trace(
        trace_id="c" * 32,
        started_at=t0,
        status=Status(code=StatusCode.ERROR, message="crashed before spans"),
        spans=[],
    )
    tmp_store.upsert_trace(declared)
    tmp_store.upsert_trace(declared.model_copy(deep=True))  # re-deliver
    loaded = tmp_store.get_trace("c" * 32)
    assert loaded is not None
    assert loaded.status.code == StatusCode.ERROR


def test_older_end_time_does_not_regress_and_flags_out_of_order(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """An out-of-order delivery carrying an *earlier* end_time must not
    overwrite the stored later one — it records OUT_OF_ORDER evidence."""
    tmp_store.upsert_trace(synthetic_trace)
    stored_end = synthetic_trace.spans[0].end_time

    stale = synthetic_trace.model_copy(deep=True)
    stale.spans = [stale.spans[0]]
    stale.spans[0].end_time = stored_end - timedelta(milliseconds=500)  # type: ignore[operator]
    stale.spans[0].attributes = {"late": "attr"}  # force a real merge path
    tmp_store.upsert_trace(stale)

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    span = loaded.spans[0]
    assert span.end_time == stored_end  # never regressed
    assert IntegrityFlag.OUT_OF_ORDER in span.integrity_flags
    assert span.attributes["late"] == "attr"  # merge still applied


def test_same_key_event_attributes_merge(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """A re-delivered event with the same (name, time) but richer
    attributes must merge them, not discard the difference."""
    from forge_lens.contracts.v1.trace import SpanEvent

    ev_time = synthetic_trace.spans[0].start_time
    first = synthetic_trace.model_copy(deep=True)
    first.spans = [first.spans[0]]
    first.spans[0].events = [
        SpanEvent(name="retry", time=ev_time, attributes={"attempt": 1})
    ]
    tmp_store.upsert_trace(first)

    second = synthetic_trace.model_copy(deep=True)
    second.spans = [second.spans[0]]
    second.spans[0].events = [
        SpanEvent(
            name="retry", time=ev_time,
            attributes={"attempt": 1, "backoff_ms": 250},
        )
    ]
    tmp_store.upsert_trace(second)

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    ev = loaded.spans[0].events[0]
    assert ev.attributes == {"attempt": 1, "backoff_ms": 250}
    assert len(loaded.spans[0].events) == 1  # still deduped by key


def test_span_provenance_upgrades_on_redelivery(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Span-level provenance keeps the strongest claim on merge — same
    rule as the trace row (observed > imported > derived > inferred)."""
    from forge_lens.contracts.provenance import Provenance

    weak = synthetic_trace.model_copy(deep=True)
    weak.provenance = Provenance.INFERRED
    for s in weak.spans:
        s.provenance = Provenance.INFERRED
    tmp_store.upsert_trace(weak)

    # same content with observed provenance — all 3 spans update, none
    # are mislabeled duplicates
    _, updated, _ = tmp_store.upsert_trace(synthetic_trace)
    assert updated == 3

    trace = tmp_store.get_trace(synthetic_trace.trace_id)
    assert trace is not None
    assert trace.provenance == Provenance.OBSERVED
    assert all(s.provenance == Provenance.OBSERVED for s in trace.spans)

    # and never regresses on a weaker re-delivery
    tmp_store.upsert_trace(weak)
    trace = tmp_store.get_trace(synthetic_trace.trace_id)
    assert trace is not None
    assert all(s.provenance == Provenance.OBSERVED for s in trace.spans)


def test_status_derived_from_spans_on_insert_idempotent(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """W1: INSERT must bind the same span-derived status the UPDATE
    CASE computes — declared-vs-derived divergence used to flip state
    on every identical re-delivery."""
    # declared ERROR + all-OK spans → stored 'ok' from write #1
    t = synthetic_trace.model_copy(deep=True)
    for s in t.spans:
        s.status.code = StatusCode.OK
    t.status.code = StatusCode.ERROR
    tmp_store.upsert_trace(t)
    s1 = tmp_store.get_trace(t.trace_id)
    assert s1.status.code == StatusCode.OK
    tmp_store.upsert_trace(t)  # identical re-delivery
    assert tmp_store.get_trace(t.trace_id).status.code == StatusCode.OK

    # declared OK + an ERROR span → 'error' + error_count on write #1
    t2 = synthetic_trace.model_copy(deep=True)
    t2.trace_id = "d" * 32
    for s in t2.spans:
        s.trace_id = t2.trace_id
    t2.status.code = StatusCode.OK
    tmp_store.upsert_trace(t2)
    s2 = tmp_store.get_trace(t2.trace_id)
    assert s2.status.code == StatusCode.ERROR
    listed = tmp_store.list_traces(project_id="default", status="error")
    assert any(i["trace_id"] == t2.trace_id for i in listed)


def test_naive_event_time_normalized_on_direct_upsert(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """W2: upsert_trace is the public contract — a naive SpanEvent.time
    must normalize like the pipeline does, or replay 500s."""
    from forge_lens.contracts.v1.trace import SpanEvent
    from forge_lens.investigate import replay_timeline

    t = synthetic_trace.model_copy(deep=True)
    naive_time = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC).replace(tzinfo=None)
    t.spans[0].events = [SpanEvent(name="e", time=naive_time)]
    tmp_store.upsert_trace(t)
    stored = tmp_store.get_trace(t.trace_id)
    assert stored.spans[0].events[0].time.tzinfo is not None
    tl = replay_timeline(stored)
    assert any(e["kind"] == "event" for e in tl["entries"])


def test_non_finite_attribute_stored_as_null_not_invalid_json(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """W3: NaN/Infinity have no JSON literal — persisted as null, and
    the stored document stays json_valid (verified in sqlite itself)."""
    t = synthetic_trace.model_copy(deep=True)
    t.spans[0].attributes["temperature"] = float("nan")
    t.resource_attributes["threshold"] = float("inf")
    tmp_store.upsert_trace(t)
    row = tmp_store._conn.execute(
        "SELECT json_valid(attributes_json) AS va, "
        "       json_valid((SELECT resource_attributes_json FROM traces "
        "                  WHERE trace_id=?)) AS vr "
        "FROM spans WHERE trace_id=? AND span_id=?",
        (t.trace_id, t.trace_id, t.spans[0].span_id),
    ).fetchone()
    assert row["va"] == 1
    assert row["vr"] == 1
    stored = tmp_store.get_trace(t.trace_id)
    assert stored.spans[0].attributes["temperature"] is None
    assert stored.resource_attributes["threshold"] is None


def test_list_traces_deterministic_tiebreak_on_same_instant(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Paginated ORDER BY started_ns DESC needs a total order — same
    instant must fall back to trace_id or pages can interleave."""
    t0 = synthetic_trace.started_at
    a = synthetic_trace.model_copy(deep=True)
    a.trace_id = "a" * 32
    for s in a.spans:
        s.trace_id = a.trace_id
    a.started_at = t0
    b = synthetic_trace.model_copy(deep=True)
    b.trace_id = "b" * 32
    for s in b.spans:
        s.trace_id = b.trace_id
    b.started_at = t0
    tmp_store.upsert_trace(b)  # inserted second, same instant
    tmp_store.upsert_trace(a)
    items = tmp_store.list_traces()
    ids = [i["trace_id"] for i in items]
    assert ids == sorted(ids)  # trace_id ASC within equal started_ns
    assert items == tmp_store.list_traces()  # stable across calls


def test_dataset_store(tmp_store: SQLiteTraceStore) -> None:
    tmp_store.upsert_dataset(
        "qa", project_id="p1", name="QA set", task="answer questions",
        metadata={"owner": "evals"}, now_ns=1_000,
    )
    ds = tmp_store.get_dataset("qa", "p1")
    assert ds is not None and ds["name"] == "QA set"
    assert ds["metadata"]["owner"] == "evals"
    assert tmp_store.get_dataset("qa", "other") is None

    n = tmp_store.upsert_dataset_version(
        "qa", "v1", project_id="p1", description="first",
        cases=[
            {"case_id": "c1", "expected": {"answer": "42"},
             "allowed_tools": ["web_search"]},
            {"case_id": "c2", "input": {"q": "x"}},
        ],
        now_ns=2_000,
    )
    assert n == 2
    versions = tmp_store.list_dataset_versions("qa", "p1")
    assert len(versions) == 1 and versions[0]["case_count"] == 2
    v = tmp_store.get_dataset_version("qa", "v1", "p1")
    assert v is not None
    assert [c["case_id"] for c in v["cases"]] == ["c1", "c2"]
    assert v["cases"][0]["allowed_tools"] == ["web_search"]
    assert tmp_store.get_dataset_version("qa", "v9", "p1") is None
    assert [d["dataset_id"] for d in tmp_store.list_datasets()] == ["qa"]
