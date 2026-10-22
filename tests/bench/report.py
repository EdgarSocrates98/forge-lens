"""Timing collector shared by the bench fixtures, tests and hooks."""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from forge_lens.storage.sqlite import SQLiteTraceStore


def percentile(samples: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile (numpy's default rule).

    Deterministic given the sample set — used to collapse per-batch /
    per-request latency distributions into p50/p95/p99 detail fields.
    """
    s = sorted(samples)
    if not s:
        return 0.0
    if len(s) == 1:
        return s[0]
    rank = (q / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def median(samples: Sequence[float]) -> float:
    return percentile(samples, 50.0)


@dataclass
class BenchRecord:
    surface: str
    seconds: float
    detail: dict[str, Any] = field(default_factory=dict)


class BenchReport:
    """Ordered log of wall-time measurements for one pytest session."""

    def __init__(self) -> None:
        self.records: list[BenchRecord] = []

    def record(self, surface: str, seconds: float, **detail: Any) -> float:
        """Append a measurement; returns ``seconds`` for ceiling asserts."""
        self.records.append(BenchRecord(surface, seconds, detail))
        return seconds

    @contextmanager
    def time(self, surface: str, **detail: Any) -> Iterator[None]:
        """Time a block whose detail is known up front::

            with bench.time("store.list_sessions", seeded_traces=1000):
                rows = store.list_sessions()
        """
        t0 = time.perf_counter()
        yield
        self.record(surface, time.perf_counter() - t0, **detail)


@dataclass
class SeededCorpus:
    """The shared 1 000-trace store plus its known dimensions."""

    store: SQLiteTraceStore
    project_id: str
    traces: int
    spans: int
    db_bytes: int
