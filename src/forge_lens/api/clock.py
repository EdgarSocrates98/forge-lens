"""Boundary wall clock — the single documented seam for real time.

The deterministic core never reads a clock (AGENTS.md hard rule): trace
timestamps are *data*, and engines take injected ``now``. Real wall
time is legitimately needed only at the I/O boundary — audit-log
timestamps, retention cutoffs — and lives exclusively here in ``api/``
(the allowed zone per AGENTS.md) so it can be audited, injected in
tests, and never leaks into engine modules.
"""

from __future__ import annotations

import time


def wall_clock_ns() -> int:
    """Real Unix nanoseconds — boundary code only, never engine math."""
    return time.time_ns()
