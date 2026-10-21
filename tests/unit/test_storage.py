from __future__ import annotations

from forge_lens.contracts.v1.trace import StatusCode, Trace
from forge_lens.storage.sqlite import SQLiteTraceStore


def test_upsert_and_get_roundtrip(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    inserted, dup = tmp_store.upsert_trace(synthetic_trace)
    assert inserted == 3
    assert dup == 0

    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert loaded is not None
    assert loaded.trace_id == synthetic_trace.trace_id
    assert len(loaded.spans) == 3
    llm = next(s for s in loaded.spans if s.kind.value == "llm")
    assert llm.attributes["forge_lens.token_usage"]["input_tokens"] == 100
    assert loaded.status.code == StatusCode.ERROR  # tool span failed


def test_reingest_is_idempotent(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    tmp_store.upsert_trace(synthetic_trace)
    inserted, dup = tmp_store.upsert_trace(synthetic_trace)
    assert inserted == 0
    assert dup == 3
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert len(loaded.spans) == 3  # still 3, no duplication


def test_partial_flush_merges(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    first = synthetic_trace.model_copy(deep=True)
    first.spans = synthetic_trace.spans[:1]
    tmp_store.upsert_trace(first)
    second = synthetic_trace.model_copy(deep=True)
    second.spans = synthetic_trace.spans[1:]
    tmp_store.upsert_trace(second)
    loaded = tmp_store.get_trace(synthetic_trace.trace_id)
    assert len(loaded.spans) == 3


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
