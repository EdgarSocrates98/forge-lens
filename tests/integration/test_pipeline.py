"""Pipeline-level tests: limits, dedupe, integrity flags, rate limiting."""

from __future__ import annotations

import pytest

from forge_lens.contracts.v1.trace import IntegrityFlag, Trace
from forge_lens.core.config import LensConfig
from forge_lens.core.errors import PayloadTooLargeError, RateLimitedError
from forge_lens.ingest.pipeline import IngestPipeline, RateLimiter, flag_integrity
from forge_lens.storage.sqlite import SQLiteTraceStore


def _pipeline(tmp_store: SQLiteTraceStore, **kw) -> IngestPipeline:
    return IngestPipeline(tmp_store, LensConfig(**kw))


def test_end_to_end_ingest(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    result = _pipeline(tmp_store).ingest([synthetic_trace])
    assert result.accepted_spans == 3
    assert result.duplicate_spans == 0
    assert tmp_store.get_trace(synthetic_trace.trace_id) is not None


def test_dedupe_on_reingest(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    p = _pipeline(tmp_store)
    p.ingest([synthetic_trace])
    result = p.ingest([synthetic_trace])
    assert result.accepted_spans == 0
    assert result.duplicate_spans == 3


def test_payload_limits(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    p = _pipeline(tmp_store, max_payload_bytes=10)
    with pytest.raises(PayloadTooLargeError):
        p.ingest([synthetic_trace], payload_bytes=100)


def test_span_count_limit(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    p = _pipeline(tmp_store, max_spans_per_request=2)
    with pytest.raises(PayloadTooLargeError):
        p.ingest([synthetic_trace])


def test_rate_limiter() -> None:
    limiter = RateLimiter(per_minute=2)
    assert limiter.allow()
    assert limiter.allow()
    assert not limiter.allow()
    unlimited = RateLimiter(per_minute=0)
    for _ in range(10):
        assert unlimited.allow()


def test_rate_limiter_in_pipeline(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    p = _pipeline(tmp_store, rate_limit_per_minute=1)
    p.ingest([synthetic_trace])
    with pytest.raises(RateLimitedError):
        p.ingest([synthetic_trace])


def test_integrity_flags_missing_parent(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    partial = synthetic_trace.model_copy(deep=True)
    partial.spans = [partial.spans[1]]  # llm span, parent not in batch
    result = _pipeline(tmp_store).ingest([partial])
    stored = tmp_store.get_trace(partial.trace_id)
    assert IntegrityFlag.MISSING_PARENT in stored.spans[0].integrity_flags
    assert result.warnings  # partial trace warning


def test_missing_end_flagged(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    running = synthetic_trace.model_copy(deep=True)
    running.spans[0].end_time = None
    flag_integrity(running)
    assert IntegrityFlag.MISSING_END in running.spans[0].integrity_flags


def test_event_cap_flags_truncated(tmp_store: SQLiteTraceStore, synthetic_trace: Trace) -> None:
    from datetime import UTC, datetime

    from forge_lens.contracts.v1.trace import SpanEvent

    big = synthetic_trace.model_copy(deep=True)
    big.spans[0].events = [
        SpanEvent(name=f"e{i}", time=datetime.now(UTC)) for i in range(10)
    ]
    _pipeline(tmp_store, max_events_per_span=3).ingest([big])
    stored = tmp_store.get_trace(big.trace_id)
    assert len(stored.spans[0].events) == 3
    assert IntegrityFlag.TRUNCATED in stored.spans[0].integrity_flags
