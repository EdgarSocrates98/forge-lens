"""Ingestion pipeline: validate → sanitize → flag-integrity → dedupe → persist.

The pipeline is the only path to the store. Every stage is explicit and
its decisions are recorded on the trace itself (integrity flags), in the
``IngestResult`` and in self-metrics — never silently.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from forge_lens.contracts.v1.trace import IntegrityFlag, Trace
from forge_lens.core.config import LensConfig
from forge_lens.core.errors import PayloadTooLargeError, RateLimitedError, ValidationError
from forge_lens.core.metrics import (
    M_EVENTS_INGESTED,
    M_INGEST_DUPLICATES,
    M_INGEST_LATENCY,
    M_INGEST_REJECTED,
    M_SPANS_INGESTED,
    M_TRACES_INGESTED,
    METRICS,
)
from forge_lens.sanitize.redact import SanitizeReport, sanitize_trace
from forge_lens.storage.base import TraceStore


class RateLimiter:
    """Fixed-window in-process limiter (local-first single user)."""

    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self._lock = threading.Lock()
        self._window_start = time.monotonic()
        self._count = 0

    def allow(self) -> bool:
        if self.per_minute <= 0:
            return True
        with self._lock:
            now = time.monotonic()
            if now - self._window_start >= 60.0:
                self._window_start = now
                self._count = 0
            self._count += 1
            return self._count <= self.per_minute


@dataclass
class IngestResult:
    accepted_spans: int = 0
    updated_spans: int = 0
    duplicate_spans: int = 0
    traces: list[str] = field(default_factory=list)
    sanitize: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def flag_integrity(trace: Trace) -> list[str]:
    """Attach non-fatal data-quality flags. Returns human-readable warnings."""
    warnings: list[str] = []
    span_ids = {s.span_id for s in trace.spans}
    missing_end = 0
    missing_parents = 0
    for span in trace.spans:
        if span.parent_span_id and span.parent_span_id not in span_ids:
            missing_parents += 1
            if IntegrityFlag.MISSING_PARENT not in span.integrity_flags:
                span.integrity_flags.append(IntegrityFlag.MISSING_PARENT)
        if span.end_time is None:
            missing_end += 1
            if IntegrityFlag.MISSING_END not in span.integrity_flags:
                span.integrity_flags.append(IntegrityFlag.MISSING_END)
        if (
            span.end_time
            and span.end_time < span.start_time
            and IntegrityFlag.CLOCK_SKEW not in span.integrity_flags
        ):
            span.integrity_flags.append(IntegrityFlag.CLOCK_SKEW)
    if not trace.spans:
        warnings.append("trace contains no spans")
    elif missing_end == len(trace.spans):
        warnings.append("trace has no span end times — possibly still running")
    if missing_parents:
        warnings.append(f"{missing_parents} span(s) reference parents not present in this trace")
    if trace.ended_at is None and trace.spans:
        warnings.append("trace is partial (no ended_at)")
    return warnings


class IngestPipeline:
    """End-to-end ingestion for already-normalized Traces."""

    def __init__(self, store: TraceStore, config: LensConfig | None = None) -> None:
        self.store = store
        self.config = config or LensConfig()
        self._rate = RateLimiter(self.config.rate_limit_per_minute)

    def ingest(
        self,
        traces: list[Trace],
        *,
        payload_bytes: int | None = None,
    ) -> IngestResult:
        if not self._rate.allow():
            METRICS.inc(M_INGEST_REJECTED)
            raise RateLimitedError("ingestion rate limit exceeded")
        if payload_bytes is not None and payload_bytes > self.config.max_payload_bytes:
            METRICS.inc(M_INGEST_REJECTED)
            raise PayloadTooLargeError(
                f"payload {payload_bytes}B exceeds limit {self.config.max_payload_bytes}B",
                self.config.max_payload_bytes,
            )

        total_spans = sum(len(t.spans) for t in traces)
        if total_spans > self.config.max_spans_per_request:
            METRICS.inc(M_INGEST_REJECTED)
            raise PayloadTooLargeError(
                f"{total_spans} spans exceeds limit {self.config.max_spans_per_request}",
                self.config.max_spans_per_request,
            )

        # Contract consistency is checked for the whole batch before any
        # write: a span naming a different trace_id than its enclosing
        # Trace would otherwise be stored under a key nothing can query.
        for trace in traces:
            bad = [s.span_id for s in trace.spans if s.trace_id != trace.trace_id]
            if bad:
                METRICS.inc(M_INGEST_REJECTED)
                raise ValidationError(
                    f"trace {trace.trace_id}: {len(bad)} span(s) declare a different "
                    f"trace_id ({bad[:5]}) — envelope is inconsistent"
                )

        result = IngestResult()
        with METRICS.timer(M_INGEST_LATENCY):
            for trace in traces:
                for span in trace.spans:
                    if len(span.events) > self.config.max_events_per_span:
                        span.events = span.events[: self.config.max_events_per_span]
                        if IntegrityFlag.TRUNCATED not in span.integrity_flags:
                            span.integrity_flags.append(IntegrityFlag.TRUNCATED)

                report: SanitizeReport = sanitize_trace(
                    trace,
                    capture=self.config.capture,
                    max_chars=self.config.max_attr_value_chars,
                )
                warnings = flag_integrity(trace)
                result.warnings.extend(f"{trace.trace_id}: {w}" for w in warnings)

                inserted, updated, duplicates = self.store.upsert_trace(trace)
                result.accepted_spans += inserted
                result.updated_spans += updated
                result.duplicate_spans += duplicates
                result.traces.append(trace.trace_id)
                for key in (
                    "redacted_keys",
                    "redacted_values",
                    "dropped_content",
                    "truncated_values",
                    "emails_masked",
                ):
                    result.sanitize[key] = result.sanitize.get(key, 0) + getattr(report, key)

        METRICS.inc(M_TRACES_INGESTED, len(traces))
        METRICS.inc(M_SPANS_INGESTED, result.accepted_spans)
        METRICS.inc(
            M_EVENTS_INGESTED,
            sum(len(s.events) for t in traces for s in t.spans),
        )
        METRICS.inc(M_INGEST_DUPLICATES, result.duplicate_spans)
        return result
