"""Time helpers — UTC everywhere, exact integer nanosecond math.

All conversions are integer-only: ``datetime`` has microsecond
resolution, so sub-microsecond nanos are truncated on the way in
(documented, deterministic — never silently rounded by float error).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

NANOS_PER_SECOND = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


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


def ensure_aware(dt: datetime) -> datetime:
    """Coerce a naive datetime to UTC-aware (naive-as-UTC).

    Same convention as :func:`to_unix_nano`: a timestamp with no offset
    is read as UTC — never rejected, never re-interpreted as local time.
    Call this before any cross-datetime comparison/min/max so mixed
    naive/aware input can't raise ``TypeError``.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
