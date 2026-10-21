"""Pipeline-level tests: limits, dedupe, integrity flags, rate limiting."""

from __future__ import annotations

from datetime import timedelta

import pytest

from forge_lens.contracts.v1.trace import IntegrityFlag, Trace
from forge_lens.core.config import LensConfig
from forge_lens.core.errors import PayloadTooLargeError, RateLimitedError, ValidationError
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


def test_rate_limiter_uses_injected_clock() -> None:
    """The window boundary is deterministic: a fake clock controls when
    the bucket resets — no sleeping in tests, no wall-clock reads."""
    now = [1_000.0]
    limiter = RateLimiter(per_minute=2, clock=lambda: now[0])
    assert limiter.allow()
    assert limiter.allow()
    assert not limiter.allow()
    now[0] += 59.0  # still inside the window
    assert not limiter.allow()
    now[0] += 1.0  # window rolled
    assert limiter.allow()


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


def test_span_trace_id_mismatch_rejected(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """A span naming another trace would black-hole data — reject before write."""
    bad = synthetic_trace.model_copy(deep=True)
    bad.spans[1].trace_id = "d" * 32
    with pytest.raises(ValidationError, match="different"):
        _pipeline(tmp_store).ingest([bad])
    assert tmp_store.get_trace(synthetic_trace.trace_id) is None  # nothing written
    assert tmp_store.get_trace("d" * 32) is None


def test_empty_trace_no_misleading_warning(tmp_store: SQLiteTraceStore) -> None:
    """An empty trace warns 'no spans' — not 'no span end times'."""
    from datetime import UTC, datetime

    empty = Trace(
        trace_id="e" * 32,
        started_at=datetime.now(UTC),
        spans=[],
    )
    result = _pipeline(tmp_store).ingest([empty])
    assert any("no spans" in w for w in result.warnings)
    assert not any("no span end times" in w for w in result.warnings)


def test_late_end_time_update_counts_updated(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Re-delivering a running span with end_time → updated, not duplicate."""
    running = synthetic_trace.model_copy(deep=True)
    running.spans = [running.spans[0]]
    running.spans[0].end_time = None
    p = _pipeline(tmp_store)
    r1 = p.ingest([running])
    assert r1.accepted_spans == 1

    completed = synthetic_trace.model_copy(deep=True)
    completed.spans = [completed.spans[0]]
    r2 = p.ingest([completed])
    assert r2.updated_spans == 1
    assert r2.accepted_spans == 0
    assert r2.duplicate_spans == 0

    r3 = p.ingest([completed])
    assert r3.duplicate_spans == 1  # identical re-delivery is a duplicate


def test_max_traces_per_request_bounded(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Trace-count bound is enforced separately from the span bound —
    many empty traces can't flood the store."""
    p = _pipeline(tmp_store, max_traces_per_request=3)
    empty = synthetic_trace.model_copy(deep=True)
    empty.spans = []
    batch = [
        empty.model_copy(update={"trace_id": f"{i:032x}"}, deep=True)
        for i in range(4)
    ]
    with pytest.raises(PayloadTooLargeError):
        p.ingest(batch)
    stats = tmp_store.stats()
    assert stats["traces"] == 0


def test_naive_times_normalized_to_utc(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Mixed naive/aware datetimes can't crash flag_integrity — the
    pipeline coerces naive → UTC at the boundary (to_unix_nano parity)."""
    mixed = synthetic_trace.model_copy(deep=True)
    s = mixed.spans[0]
    s.start_time = s.start_time.replace(tzinfo=None)
    s.end_time = s.end_time.replace(tzinfo=None) if s.end_time else None
    mixed.started_at = mixed.started_at.replace(tzinfo=None)
    p = _pipeline(tmp_store)
    r = p.ingest([mixed])
    assert r.accepted_spans == 3
    loaded = tmp_store.get_trace(mixed.trace_id)
    assert loaded is not None
    # naive-as-UTC: stored timestamps equal the aware originals
    assert loaded.spans[0].start_time == synthetic_trace.spans[0].start_time


def test_declared_bounds_contradicting_spans_warns(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Envelope-declared started_at/ended_at are trusted as-is — but a
    contradiction with the actual span bounds must surface as evidence."""
    bad = synthetic_trace.model_copy(deep=True)
    # spans start at t0; declare a started_at 10s later
    bad.started_at = bad.spans[0].start_time + timedelta(seconds=10)
    # and an ended_at before the latest span end
    bad.ended_at = bad.spans[0].start_time
    result = _pipeline(tmp_store).ingest([bad])
    assert any("started_at" in w for w in result.warnings)
    assert any("ended_at" in w for w in result.warnings)


# -- round-10: dead contract flags wired -------------------------------------


def test_llm_span_without_usage_gets_unknown_tokens(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    from forge_lens.contracts.v1.trace import SpanKind

    # synthetic_trace's llm span carries usage; strip it to trigger the flag
    bare = synthetic_trace.model_copy(deep=True)
    bare.ended_at = bare.spans[-1].end_time  # avoid partial noise
    for s in bare.spans:
        if s.kind is SpanKind.LLM:
            s.attributes.pop("forge_lens.token_usage")
    _pipeline(tmp_store).ingest([bare])
    stored = tmp_store.get_trace(bare.trace_id)
    llm = next(s for s in stored.spans if s.kind is SpanKind.LLM)
    assert IntegrityFlag.UNKNOWN_TOKENS in llm.integrity_flags


def test_cross_trace_parent_gets_orphan(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """parent_span_id that exists under ANOTHER stored trace → ORPHAN."""
    from datetime import UTC, datetime

    from forge_lens.contracts.v1.trace import Span

    p = _pipeline(tmp_store)
    p.ingest([synthetic_trace])  # parent "1"*16 now lives in trace "a"*32
    orphan_span = Span(
        trace_id="b" * 32,
        span_id="9" * 16,
        parent_span_id="1" * 16,  # belongs to synthetic_trace
        name="orphan-child",
        start_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
    )
    t2 = Trace(
        trace_id="b" * 32,
        project_id=synthetic_trace.project_id,
        started_at=orphan_span.start_time,
        ended_at=orphan_span.end_time,
        spans=[orphan_span],
    )
    result = p.ingest([t2])
    stored = tmp_store.get_trace("b" * 32)
    flags = stored.spans[0].integrity_flags
    assert IntegrityFlag.ORPHAN in flags
    assert IntegrityFlag.MISSING_PARENT in flags  # additive, not replacing
    assert any("belongs to trace" in w for w in result.warnings)


def test_partial_trace_flags_root_span(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    partial = synthetic_trace.model_copy(deep=True)
    partial.ended_at = None
    _pipeline(tmp_store).ingest([partial])
    stored = tmp_store.get_trace(partial.trace_id)
    root = next(s for s in stored.spans if s.span_id == stored.root_span_id)
    assert IntegrityFlag.PARTIAL_TRACE in root.integrity_flags


def test_orphan_cleared_when_parent_lands_in_trace(
    tmp_store: SQLiteTraceStore,
) -> None:
    """ORPHAN is stale once the parent arrives *in the same trace*."""
    from datetime import UTC, datetime

    from forge_lens.contracts.v1.trace import Span, Trace

    p = _pipeline(tmp_store)
    child = Span(
        trace_id="b" * 32, span_id="9" * 16, parent_span_id="1" * 16,
        name="child",
        start_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
    )
    # the "foreign" parent first — under a different trace
    foreign_parent = Span(
        trace_id="a" * 32, span_id="1" * 16, name="foreign-parent",
        start_time=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 11, 0, 1, tzinfo=UTC),
    )
    p.ingest([
        Trace(trace_id="a" * 32, project_id="default",
              started_at=foreign_parent.start_time,
              ended_at=foreign_parent.end_time, spans=[foreign_parent]),
        Trace(trace_id="b" * 32, project_id="default",
              started_at=child.start_time, ended_at=child.end_time,
              spans=[child]),
    ])
    flags = tmp_store.get_trace("b" * 32).spans[0].integrity_flags
    assert IntegrityFlag.ORPHAN in flags

    # now the parent is delivered INTO trace b — orphan resolves
    own_parent = foreign_parent.model_copy(update={"trace_id": "b" * 32})
    p.ingest([
        Trace(trace_id="b" * 32, project_id="default",
              started_at=own_parent.start_time, ended_at=child.end_time,
              spans=[own_parent, child.model_copy()]),
    ])
    flags2 = {
        s.span_id: s.integrity_flags for s in tmp_store.get_trace("b" * 32).spans
    }["9" * 16]
    assert IntegrityFlag.ORPHAN not in flags2
    assert IntegrityFlag.MISSING_PARENT not in flags2


def test_parent_already_in_same_trace_no_false_orphan(
    tmp_store: SQLiteTraceStore,
) -> None:
    """span_id present in this trace AND another → not an orphan."""
    from datetime import UTC, datetime

    from forge_lens.contracts.v1.trace import Span, Trace

    p = _pipeline(tmp_store)
    parent = Span(
        trace_id="a" * 32, span_id="1" * 16, name="parent",
        start_time=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 11, 0, 1, tzinfo=UTC),
    )
    p.ingest([
        Trace(trace_id="a" * 32, project_id="default",
              started_at=parent.start_time, ended_at=parent.end_time,
              spans=[parent]),
        # trace b already contains its own copy of the parent from an
        # earlier delivery — but only a root so far
        Trace(trace_id="b" * 32, project_id="default",
              started_at=parent.start_time, ended_at=parent.end_time,
              spans=[parent.model_copy(update={"trace_id": "b" * 32})]),
    ])
    child = Span(
        trace_id="b" * 32, span_id="9" * 16, parent_span_id="1" * 16,
        name="child",
        start_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
    )
    # NOTE: child's parent IS in the same trace already — but the
    # incoming trace's own span set doesn't contain it, so the pipeline
    # consults the store; same-trace row must win → no ORPHAN
    p.ingest([
        Trace(trace_id="b" * 32, project_id="default",
              started_at=child.start_time, ended_at=child.end_time,
              spans=[child]),
    ])
    flags = {
        s.span_id: s.integrity_flags for s in tmp_store.get_trace("b" * 32).spans
    }["9" * 16]
    assert IntegrityFlag.ORPHAN not in flags


def test_llm_span_malformed_usage_gets_unknown_tokens(
    tmp_store: SQLiteTraceStore,
) -> None:
    """A present-but-unparseable usage dict is as unknown as none."""
    from datetime import UTC, datetime

    from forge_lens.contracts.v1.trace import Span, SpanKind, Trace

    bad_usage = Span(
        trace_id="c" * 32, span_id="1" * 16, name="gen",
        kind=SpanKind.LLM,
        start_time=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        end_time=datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC),
        attributes={"forge_lens.token_usage": {"model": 42, "input_tokens": "x"}},
    )
    p = _pipeline(tmp_store)
    p.ingest([
        Trace(trace_id="c" * 32, project_id="default",
              started_at=bad_usage.start_time, ended_at=bad_usage.end_time,
              spans=[bad_usage]),
    ])
    stored = tmp_store.get_trace("c" * 32)
    assert IntegrityFlag.UNKNOWN_TOKENS in stored.spans[0].integrity_flags


def test_divergent_parent_is_conflict_not_duplicate(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """Re-delivery claiming a different parent → CONFLICT, kept parent."""
    from datetime import UTC, datetime

    from forge_lens.contracts.v1.trace import Span

    p = _pipeline(tmp_store)
    p.ingest([synthetic_trace])
    target = synthetic_trace.spans[1]  # llm span — has parent "1"*16
    diverged = Span(
        trace_id=target.trace_id,
        span_id=target.span_id,
        parent_span_id="f" * 16,  # different claimed parent
        name=target.name,
        kind=target.kind,
        start_time=target.start_time,
        end_time=datetime(2026, 1, 1, 12, 0, 2, tzinfo=UTC),
    )
    t2 = Trace(
        trace_id=target.trace_id,
        project_id=synthetic_trace.project_id,
        started_at=target.start_time,
        ended_at=diverged.end_time,
        spans=[diverged],
    )
    result = p.ingest([t2])
    stored = tmp_store.get_trace(target.trace_id)
    span = next(s for s in stored.spans if s.span_id == target.span_id)
    assert IntegrityFlag.CONFLICT in span.integrity_flags
    assert span.parent_span_id == target.parent_span_id  # stored wins
    assert result.duplicate_spans + result.updated_spans >= 1


def test_producer_flags_stripped_at_ingest(
    tmp_store: SQLiteTraceStore, synthetic_trace: Trace
) -> None:
    """A producer asserting conflict/duplicate on wire data fabricates
    evidence — flags are engine verdicts, recomputed at ingest."""
    forged = synthetic_trace.model_copy(deep=True)
    for s in forged.spans:
        s.integrity_flags = [IntegrityFlag.CONFLICT, IntegrityFlag.DUPLICATE]
    _pipeline(tmp_store).ingest([forged])
    stored = tmp_store.get_trace(forged.trace_id)
    for s in stored.spans:
        assert IntegrityFlag.CONFLICT not in s.integrity_flags
        assert IntegrityFlag.DUPLICATE not in s.integrity_flags
