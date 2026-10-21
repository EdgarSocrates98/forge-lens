"""Storage protocol — the seam between the pipeline and backends.

SQLite is the local-first default backend. The protocol is intentionally
narrow so alternative backends (e.g. Parquet export, ClickHouse in a
later cycle) can implement it without touching the pipeline.
"""

from __future__ import annotations

from typing import Any, Protocol

from forge_lens.contracts.v1.trace import Span, Trace


class TraceStore(Protocol):
    """What the rest of the platform needs from a trace backend."""

    def init_schema(self) -> None: ...

    def upsert_trace(self, trace: Trace) -> tuple[int, int]:
        """Persist a trace. Returns ``(inserted_spans, duplicate_spans)``."""
        ...

    def get_trace(self, trace_id: str, project_id: str | None = None) -> Trace | None: ...

    def list_traces(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def stats(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class SpanWriter(Protocol):
    def insert_spans(self, spans: list[Span]) -> tuple[int, int]: ...
