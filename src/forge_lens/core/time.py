"""Time helpers — UTC everywhere, exact integer nanosecond math.

All conversions are integer-only: ``datetime`` has microsecond
resolution, so sub-microsecond nanos are truncated on the way in
(documented, deterministic — never silently rounded by float error).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_lens.core.errors import ValidationError

# SQLite INTEGER is signed int64 — a datetime outside ~1678-2262
# nanos-since-epoch can never be persisted; reject it as a domain
# validation error (→ 4xx) rather than letting sqlite3.OverflowError
# surface as a 500.
_I64_MAX = 2**63 - 1

NANOS_PER_SECOND = 1_000_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def to_unix_nano(dt: datetime) -> int:
    """UTC-aware datetime → nanoseconds since epoch (exact, no float)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - _EPOCH
    ns = (
        delta.days * 86_400 * NANOS_PER_SECOND
        + delta.seconds * NANOS_PER_SECOND
        + delta.microseconds * 1_000
    )
    if abs(ns) > _I64_MAX:
        raise ValidationError(
            f"datetime {dt.isoformat()} is outside the int64 "
            "nanosecond range (~1678-2262)"
        )
    return ns


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
