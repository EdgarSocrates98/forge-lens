"""Self-observability for Forge Lens itself.

The observability system is observable: ingestion rate, queue depth,
dropped events, query latency and API errors are counters/gauges exposed
on ``GET /v1/metrics``. In-process only for Cycle 01; an OTLP exporter
for the Lens itself is a later-cycle decision (and must avoid recursive
self-tracing).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Metrics:
    """Thread-safe in-process metrics registry."""

    counters: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    gauges: dict[str, float] = field(default_factory=dict)
    # name -> list of observations (bounded)
    histograms: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    histogram_capacity: int = 1024
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] += amount

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self.gauges[name] = value

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            series = self.histograms[name]
            series.append(value)
            if len(series) > self.histogram_capacity:
                del series[: len(series) - self.histogram_capacity]

    @contextmanager
    def timer(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.observe(name, (time.perf_counter() - start) * 1000.0)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            hist = {
                name: {
                    "count": len(series),
                    "min_ms": min(series) if series else None,
                    "max_ms": max(series) if series else None,
                    "avg_ms": (sum(series) / len(series)) if series else None,
                }
                for name, series in self.histograms.items()
            }
            return {
                "counters": dict(self.counters),
                "gauges": dict(self.gauges),
                "histograms": hist,
            }


METRICS = Metrics()

# Canonical metric names (bounded label-free namespace).
M_SPANS_INGESTED = "lens.ingest.spans"
M_TRACES_INGESTED = "lens.ingest.traces"
M_EVENTS_INGESTED = "lens.ingest.events"
M_INGEST_DUPLICATES = "lens.ingest.duplicates"
M_INGEST_REJECTED = "lens.ingest.rejected"
M_INGEST_ERRORS = "lens.ingest.errors"
M_ATTRS_REDACTED = "lens.sanitize.attrs_redacted"
M_ATTRS_TRUNCATED = "lens.sanitize.attrs_truncated"
M_API_REQUESTS = "lens.api.requests"
M_API_ERRORS = "lens.api.errors"
M_QUERY_LATENCY = "lens.query.latency_ms"
M_INGEST_LATENCY = "lens.ingest.latency_ms"
