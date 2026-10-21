"""W3C Trace Context identity helpers.

``trace_id`` is 16 bytes (32 lowercase hex chars), ``span_id`` is 8 bytes
(16 lowercase hex chars). These helpers generate and validate — never
"fix" a malformed id, which would hide producer bugs.
"""

from __future__ import annotations

import re
import secrets

TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
TRACEPARENT_RE = re.compile(
    r"^([\da-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$"
)


def new_trace_id() -> str:
    """Random 32-hex trace id (never all zeros per W3C)."""
    tid = secrets.token_hex(16)
    while tid == "0" * 32:
        tid = secrets.token_hex(16)
    return tid


def new_span_id() -> str:
    """Random 16-hex span id (never all zeros per W3C)."""
    sid = secrets.token_hex(8)
    while sid == "0" * 16:
        sid = secrets.token_hex(8)
    return sid


def is_valid_trace_id(value: str) -> bool:
    return bool(TRACE_ID_RE.match(value)) and value != "0" * 32


def is_valid_span_id(value: str) -> bool:
    return bool(SPAN_ID_RE.match(value)) and value != "0" * 16


def format_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    """Build a W3C ``traceparent`` header value."""
    flags = "01" if sampled else "00"
    return f"00-{trace_id}-{span_id}-{flags}"


def parse_traceparent(header: str) -> tuple[str, str, bool] | None:
    """Parse a W3C ``traceparent`` header.

    Returns ``(trace_id, span_id, sampled)`` or ``None`` when malformed.
    Only the first 4 fields are consumed; a future flags/version field is
    tolerated per the spec (``00-<32>-<16>-<2>-extra`` is legal).
    """
    if not header:
        return None
    parts = header.strip().split("-")
    if len(parts) < 4:
        return None
    match = TRACEPARENT_RE.match("-".join(parts[:4]))
    if not match:
        return None
    _, trace_id, span_id, flags = match.groups()
    # W3C: the sampled flag is bit 0x01 of the flags byte, not the last char
    # (e.g. flags=03 → 0x03 & 0x01 → sampled).
    return trace_id, span_id, bool(int(flags, 16) & 0x01)
