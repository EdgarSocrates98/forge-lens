"""Time helpers — UTC everywhere, exact integer nanosecond math.

All conversions are integer-only: ``datetime`` has microsecond
resolution, so sub-microsecond nanos are truncated on the way in
(documented, deterministic — never silently rounded by float error).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

NANOS_PER_SECOND = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def to_unix_nano(dt: datetime) -> int:
    """UTC-aware datetime → nanoseconds since epoch (exact, no float)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - _EPOCH
    return (
        delta.days * 86_400 * NANOS_PER_SECOND
        + delta.seconds * NANOS_PER_SECOND
        + delta.microseconds * 1_000
    )


def from_unix_nano(nanos: int) -> datetime:
    """Nanoseconds since epoch → UTC datetime.

    ``datetime`` cannot hold sub-microsecond precision; the ns remainder
    is truncated (not rounded) so round-trips are stable.
    """
    seconds, rem_ns = divmod(int(nanos), NANOS_PER_SECOND)
    return _EPOCH + timedelta(seconds=seconds, microseconds=rem_ns // 1_000)


def parse_unix_nano(value: int | str) -> int:
    """OTLP sends unix nanos as a JSON string sometimes — normalize to int."""
    return int(value)
