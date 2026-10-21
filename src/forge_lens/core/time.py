"""Time helpers — nanosecond precision, UTC everywhere.

Internally we keep ``datetime`` (UTC, tz-aware) plus a parallel integer
``unix_nano`` for exact ordering — the same convention OTLP uses on the
wire.
"""

from __future__ import annotations

from datetime import UTC, datetime

NANOS_PER_SECOND = 1_000_000_000


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def to_unix_nano(dt: datetime) -> int:
    """UTC-aware datetime → nanoseconds since epoch."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * NANOS_PER_SECOND)


def from_unix_nano(nanos: int) -> datetime:
    return datetime.fromtimestamp(nanos / NANOS_PER_SECOND, tz=UTC)


def parse_unix_nano(value: int | str) -> int:
    """OTLP sends unix nanos as a JSON string sometimes — normalize to int."""
    return int(value)
