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


def test_unix_nano_roundtrip() -> None:
    now = datetime(2026, 3, 1, 10, 30, tzinfo=UTC)
    ns = to_unix_nano(now)
    assert ns == 1_772_361_000_000_000_000
    back = from_unix_nano(ns)
    assert back == now
