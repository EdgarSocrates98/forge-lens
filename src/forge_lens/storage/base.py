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

    def span_trace_id(
        self, project_id: str, span_id: str, *, trace_id: str | None = None
    ) -> str | None:
        """Which stored trace owns ``span_id`` in this project, if any —
        lets ingest mark ORPHAN for cross-trace parent references.
        When ``trace_id`` is given, a row under that trace wins (the
        parent already delivered into this trace is not an orphan); the
        result is deterministic regardless."""
        ...

    # -- datasets (Cycle 06) --------------------------------------------

    def upsert_dataset(
        self,
        dataset_id: str,
        *,
        project_id: str = "default",
        name: str,
        task: str | None = None,
        metadata: dict[str, Any] | None = None,
        now_ns: int | None = None,
    ) -> None: ...

    def get_dataset(
        self, dataset_id: str, project_id: str = "default"
    ) -> dict[str, Any] | None: ...

    def list_datasets(self, project_id: str | None = None) -> list[dict[str, Any]]: ...

    def upsert_dataset_version(
        self,
        dataset_id: str,
        version: str,
        *,
        project_id: str = "default",
        description: str | None = None,
        cases: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
        now_ns: int | None = None,
    ) -> int: ...

    def get_dataset_version(
        self, dataset_id: str, version: str, project_id: str = "default"
    ) -> dict[str, Any] | None: ...

    def list_dataset_versions(
        self, dataset_id: str, project_id: str = "default"
    ) -> list[dict[str, Any]]: ...

    # -- security & governance (Cycle 08) -------------------------------

    def create_api_key(
        self,
        key_id: str,
        *,
        name: str,
        key_hash: str,
        role: str,
        projects: list[str] | None = None,
        now_ns: int | None = None,
    ) -> None: ...

    def api_key_by_hash(self, key_hash: str) -> dict[str, Any] | None: ...

    def get_api_key(self, key_id: str) -> dict[str, Any] | None: ...

    def list_api_keys(self) -> list[dict[str, Any]]: ...

    def revoke_api_key(self, key_id: str, *, now_ns: int | None = None) -> bool: ...

    def record_audit(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
        ts_ns: int | None = None,
    ) -> None: ...

    def list_audit(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]: ...

    def prune_traces(
        self, *, before_ns: int, project_id: str | None = None
    ) -> dict[str, int]: ...

    def close(self) -> None: ...
