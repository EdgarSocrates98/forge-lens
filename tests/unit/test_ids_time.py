from __future__ import annotations

from datetime import UTC, datetime

from forge_lens.core.ids import (
    format_traceparent,
    is_valid_span_id,
    is_valid_trace_id,
    new_span_id,
    new_trace_id,
    parse_traceparent,
)
from forge_lens.core.time import from_unix_nano, to_unix_nano


def test_ids_are_w3c_shaped() -> None:
    tid, sid = new_trace_id(), new_span_id()
    assert is_valid_trace_id(tid)
    assert is_valid_span_id(sid)
    assert not is_valid_trace_id("g" * 32)
    assert not is_valid_trace_id("0" * 32)
    assert not is_valid_span_id("0" * 16)


def test_traceparent_roundtrip() -> None:
    tid, sid = new_trace_id(), new_span_id()
    header = format_traceparent(tid, sid, sampled=True)
    parsed = parse_traceparent(header)
    assert parsed == (tid, sid, True)
    assert parse_traceparent("garbage") is None
    assert parse_traceparent("") is None
    # future versions append fields — must still parse first 4
    assert parse_traceparent(f"00-{tid}-{sid}-01-extra") == (tid, sid, True)


def test_traceparent_sampled_is_a_bit_not_a_char() -> None:
    """W3C sampled = flags & 0x01 — flags '03'/'ff' are sampled, '02'/'f0' not."""
    tid, sid = new_trace_id(), new_span_id()
    assert parse_traceparent(f"00-{tid}-{sid}-03") == (tid, sid, True)
    assert parse_traceparent(f"00-{tid}-{sid}-ff") == (tid, sid, True)
    assert parse_traceparent(f"00-{tid}-{sid}-02") == (tid, sid, False)
    assert parse_traceparent(f"00-{tid}-{sid}-f0") == (tid, sid, False)
    assert parse_traceparent(f"00-{tid}-{sid}-00") == (tid, sid, False)


def test_unix_nano_roundtrip() -> None:
    now = datetime(2026, 3, 1, 10, 30, tzinfo=UTC)
    ns = to_unix_nano(now)
    assert ns == 1_772_361_000_000_000_000
    back = from_unix_nano(ns)
    assert back == now


def test_unix_nano_roundtrip_is_exact() -> None:
    """Integer math: no float drift on µs-aligned values; sub-µs truncates."""
    from datetime import timedelta

    base = datetime(2026, 3, 1, 10, 30, tzinfo=UTC)
    for micros in (0, 1, 999, 123456, 999999):
        dt = base + timedelta(microseconds=micros)
        assert to_unix_nano(from_unix_nano(to_unix_nano(dt))) == to_unix_nano(dt)
        assert from_unix_nano(to_unix_nano(dt)) == dt
    # sub-microsecond nanos truncate deterministically (datetime can't hold them)
    assert to_unix_nano(from_unix_nano(1_772_361_000_123_456_789)) % 1_000 == 0
