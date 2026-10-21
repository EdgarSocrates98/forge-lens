"""Storage protocol — the seam between the pipeline and backends.

SQLite is the local-first default backend. The protocol is intentionally
narrow so alternative backends (e.g. Parquet export, ClickHouse in a
later cycle) can implement it without touching the pipeline.
"""

from __future__ import annotations

from typing import Any, Protocol

from forge_lens.contracts.v1.trace import Trace


class TraceStore(Protocol):
    """What the rest of the platform needs from a trace backend."""

    def init_schema(self) -> None: ...

    def upsert_trace(self, trace: Trace) -> tuple[int, int, int]:
        """Persist a trace atomically.

        Returns ``(inserted, updated, duplicates)`` span counts: a
        re-delivered span whose content changed (e.g. a late
        ``end_time``) is merged → ``updated``; identical re-delivery →
        ``duplicates``.
        """
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

    def span_trace_id(self, project_id: str, span_id: str) -> str | None:
        """Which stored trace owns ``span_id`` in this project, if any —
        lets ingest mark ORPHAN for cross-trace parent references."""
        ...

    def close(self) -> None: ...
