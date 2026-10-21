"""SQLite backend — the local-first default store.

Design notes:
- WAL mode + ``busy_timeout`` for concurrent writer/reader.
- Spans and events stored as rows; attributes serialized as canonical
  JSON (queryable via ``json_extract`` when needed).
- Upsert semantics are deterministic: a span is identified by
  ``(project_id, trace_id, span_id)``; re-ingesting an identical span
  (same ``span_hash``) is a duplicate, not an error. A re-delivered span
  whose content *changed* (e.g. a late ``end_time``) is merged, not
  dropped — partial traces can complete.
- Each ``upsert_trace`` runs in a single transaction: a crash mid-ingest
  never leaves a half-written trace.
- The schema is versioned via ``lens_meta.schema_version`` so future
  migrations have a hook.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from forge_lens.contracts.provenance import Provenance
from forge_lens.contracts.v1.trace import (
    IntegrityFlag,
    Span,
    SpanEvent,
    SpanLink,
    Status,
    StatusCode,
    Trace,
    TraceSource,
)
from forge_lens.core.errors import ValidationError
from forge_lens.core.time import ensure_aware, from_unix_nano, to_unix_nano

SCHEMA_VERSION = "1.0.0"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lens_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS traces (
    trace_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    session_id TEXT,
    root_span_id TEXT,
    started_ns INTEGER NOT NULL,
    ended_ns INTEGER,
    status_code TEXT NOT NULL DEFAULT 'unset',
    status_message TEXT,
    source_json TEXT NOT NULL,
    resource_attributes_json TEXT NOT NULL DEFAULT '{}',
    provenance TEXT NOT NULL DEFAULT 'observed',
    span_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    first_seen_ns INTEGER NOT NULL,
    last_seen_ns INTEGER NOT NULL,
    PRIMARY KEY (project_id, trace_id)
);
CREATE INDEX IF NOT EXISTS idx_traces_session ON traces(project_id, session_id);
CREATE INDEX IF NOT EXISTS idx_traces_started ON traces(project_id, started_ns DESC);
CREATE INDEX IF NOT EXISTS idx_traces_status ON traces(project_id, status_code);

CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    span_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'internal',
    started_ns INTEGER NOT NULL,
    ended_ns INTEGER,
    duration_ms REAL,
    status_code TEXT NOT NULL DEFAULT 'unset',
    status_message TEXT,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    events_json TEXT NOT NULL DEFAULT '[]',
    links_json TEXT NOT NULL DEFAULT '[]',
    integrity_flags_json TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL DEFAULT 'observed',
    span_hash TEXT,
    PRIMARY KEY (project_id, trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_spans_name ON spans(project_id, name);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(project_id, kind);
CREATE INDEX IF NOT EXISTS idx_spans_status ON spans(project_id, status_code);
"""


def _ns(dt: datetime | None) -> int | None:
    return to_unix_nano(dt) if dt is not None else None


def _dt(ns: int | None) -> datetime | None:
    return from_unix_nano(ns) if ns is not None else None


def _max_dt(a: datetime | None, b: datetime | None) -> datetime | None:
    """Latest of two optional datetimes; aware-safe on mixed input."""
    if a is None:
        return b
    if b is None:
        return a
    return max(ensure_aware(a), ensure_aware(b))


_PROVENANCE_RANK = {
    Provenance.OBSERVED: 0,
    Provenance.IMPORTED: 1,
    Provenance.DERIVED: 2,
    Provenance.INFERRED: 3,
    Provenance.UNKNOWN: 4,
}


def _stronger_provenance(a: Provenance, b: Provenance) -> Provenance:
    """Strongest claim wins — same rank order as the trace upsert."""
    return a if _PROVENANCE_RANK[a] <= _PROVENANCE_RANK[b] else b


def _span_digest(span: Span) -> str:
    """Content hash of a span, excluding integrity flags (they evolve)."""
    payload = json.dumps(
        span.model_dump(mode="json", exclude={"integrity_flags"}),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _merge_status(stored: Status, incoming: Status) -> Status:
    """Highest severity wins; on a tie the latest observation wins."""
    rank = {StatusCode.ERROR: 2, StatusCode.OK: 1, StatusCode.UNSET: 0}
    return incoming if rank[incoming.code] >= rank[stored.code] else stored


def _merge_events(stored: list[SpanEvent], incoming: list[SpanEvent]) -> list[SpanEvent]:
    """Union of events by (name, timestamp); same-key attributes merge.

    Same-key events produce a *copy* — mutating the stored event in
    place would make the merged==stored no-op check invisible to the
    difference (the new attributes would never be persisted).
    """
    by_key: dict[tuple[str, str], SpanEvent] = {
        (e.name, e.time.isoformat()): e for e in stored
    }
    order = [(e.name, e.time.isoformat()) for e in stored]
    for ev in incoming:
        key = (ev.name, ev.time.isoformat())
        if key in by_key:
            stored_ev = by_key[key]
            merged_attrs = {**stored_ev.attributes, **ev.attributes}
            if merged_attrs != stored_ev.attributes:
                by_key[key] = stored_ev.model_copy(
                    update={"attributes": merged_attrs}
                )
        else:
            by_key[key] = ev
            order.append(key)
    return [by_key[k] for k in order]


def _merge_links(stored: list[SpanLink], incoming: list[SpanLink]) -> list[SpanLink]:
    """Union of links by (trace_id, span_id); attributes merge dict-wise."""
    by_id: dict[tuple[str, str], SpanLink] = {(lk.trace_id, lk.span_id): lk for lk in stored}
    order = [(lk.trace_id, lk.span_id) for lk in stored]
    for lk in incoming:
        key = (lk.trace_id, lk.span_id)
        if key in by_id:
            stored_lk = by_id[key]
            merged_attrs = {**stored_lk.attributes, **lk.attributes}
            if merged_attrs != stored_lk.attributes:
                by_id[key] = stored_lk.model_copy(
                    update={"attributes": merged_attrs}
                )
        else:
            by_id[key] = lk
            order.append(key)
    return [by_id[k] for k in order]


class SQLiteTraceStore:
    """Thread-safe SQLite trace store (WAL)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Default (deferred) isolation level so `with self._conn:` is a real
        # transaction — upsert_trace is atomic per docs/architecture/storage.md.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.init_schema()

    # -- schema ---------------------------------------------------------

    def init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO lens_meta(key, value) VALUES('schema_version', ?)",
                (SCHEMA_VERSION,),
            )

    # -- write ----------------------------------------------------------

    def upsert_trace(self, trace: Trace) -> tuple[int, int, int]:
        """Persist a trace in one transaction.

        Returns ``(inserted, updated, duplicates)``: identical re-delivery
        counts as duplicate; a changed span (e.g. a late ``end_time``) is
        merged into the stored row and counted as updated.

        Raises ``ValidationError`` if any span's ``trace_id`` differs from
        the trace's — a span stored under a different trace id would be
        unreachable through ``get_trace`` (silent data loss). The ingest
        pipeline performs the same check earlier, per batch; this is the
        store-level guarantee for direct callers.
        """
        mismatched = [s.span_id for s in trace.spans if s.trace_id != trace.trace_id]
        if mismatched:
            raise ValidationError(
                f"span trace_id mismatch: spans {mismatched!r} carry a "
                f"trace_id != trace {trace.trace_id}"
            )
        inserted = updated = duplicates = 0
        # Deterministic seen-markers derived from trace data — no wall clock.
        # ``started_at`` is required by the contract, so it is never None.
        started_ns = to_unix_nano(trace.started_at)
        ended_ns = _ns(trace.ended_at)
        seen_ns = max(started_ns, ended_ns) if ended_ns is not None else started_ns
        with self._lock, self._conn:
            for span in trace.spans:
                row = self._conn.execute(
                    "SELECT * FROM spans WHERE project_id=? AND trace_id=? AND span_id=?",
                    (trace.project_id, span.trace_id, span.span_id),
                ).fetchone()
                if row is None:
                    self._insert_span(trace, span, _span_digest(span))
                    inserted += 1
                    continue
                if row["span_hash"] == _span_digest(span):
                    # identical re-delivery → evidence that a retry happened
                    self._union_flags(row, span)
                    duplicates += 1
                    continue
                stored_span = self._span_from_row(row)
                conflict = self._identity_differs(stored_span, span)
                merged = self._merge_span(stored_span, span, trace.project_id)
                if merged is None:
                    # content merged to identical — still a re-delivery,
                    # but a name/kind/start mismatch is a CONFLICT, not a
                    # duplicate (the stored row was *not* replayed verbatim)
                    self._union_flags(row, span, conflict=conflict)
                    duplicates += 1
                    continue
                if conflict:
                    flags = set(merged.integrity_flags) | {IntegrityFlag.CONFLICT}
                    merged.integrity_flags = sorted(flags, key=lambda f: f.value)
                self._update_span(trace, merged)
                updated += 1

            # flags must reflect stored truth: a parent that arrives in a
            # later batch clears MISSING_PARENT; a completed span clears
            # MISSING_END — even when the span content itself didn't change.
            self._refresh_integrity_flags(trace)

            self._conn.execute(
                """
                INSERT INTO traces (
                    trace_id, project_id, session_id, root_span_id,
                    started_ns, ended_ns, status_code, status_message,
                    source_json, resource_attributes_json, provenance,
                    span_count, error_count, first_seen_ns, last_seen_ns
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_id, trace_id) DO UPDATE SET
                    ended_ns = MAX(
                        COALESCE(excluded.ended_ns, traces.ended_ns),
                        COALESCE(traces.ended_ns, excluded.ended_ns)),
                    started_ns = MIN(
                        COALESCE(excluded.started_ns, traces.started_ns), traces.started_ns),
                    session_id = COALESCE(traces.session_id, excluded.session_id),
                    root_span_id = COALESCE(traces.root_span_id, excluded.root_span_id),
                    status_code = COALESCE(
                        (SELECT CASE
                            WHEN SUM(s.status_code='error') > 0 THEN 'error'
                            WHEN COUNT(*) > 0 AND SUM(s.status_code='ok') = COUNT(*)
                                THEN 'ok'
                            ELSE 'unset' END
                        FROM spans s
                        WHERE s.project_id=traces.project_id
                          AND s.trace_id=traces.trace_id
                        HAVING COUNT(*) > 0),
                        NULLIF(excluded.status_code, 'unset'),
                        traces.status_code),
                    status_message = COALESCE(
                        excluded.status_message, traces.status_message),
                    source_json = CASE
                        WHEN COALESCE(
                            json_extract(excluded.source_json,'$.system'),'unknown')
                            <> 'unknown'
                        THEN excluded.source_json
                        ELSE traces.source_json END,
                    resource_attributes_json = json_patch(
                        traces.resource_attributes_json,
                        excluded.resource_attributes_json),
                    provenance = CASE MIN(
                        CASE traces.provenance
                            WHEN 'observed' THEN 0 WHEN 'imported' THEN 1
                            WHEN 'derived' THEN 2 WHEN 'inferred' THEN 3
                            ELSE 4 END,
                        CASE excluded.provenance
                            WHEN 'observed' THEN 0 WHEN 'imported' THEN 1
                            WHEN 'derived' THEN 2 WHEN 'inferred' THEN 3
                            ELSE 4 END)
                        WHEN 0 THEN 'observed' WHEN 1 THEN 'imported'
                        WHEN 2 THEN 'derived' WHEN 3 THEN 'inferred'
                        ELSE 'unknown' END,
                    span_count = (
                        SELECT COUNT(*) FROM spans
                        WHERE project_id=traces.project_id
                          AND trace_id=traces.trace_id),
                    error_count = (
                        SELECT COUNT(*) FROM spans
                        WHERE project_id=traces.project_id
                          AND trace_id=traces.trace_id
                          AND status_code='error'),
                    first_seen_ns = MIN(traces.first_seen_ns, excluded.first_seen_ns),
                    last_seen_ns = MAX(traces.last_seen_ns, excluded.last_seen_ns)
                """,
                (
                    trace.trace_id,
                    trace.project_id,
                    trace.session_id,
                    trace.root_span_id,
                    started_ns,
                    ended_ns,
                    trace.status.code.value,
                    trace.status.message,
                    trace.source.model_dump_json(),
                    json.dumps(trace.resource_attributes, sort_keys=True, default=str),
                    trace.provenance.value,
                    len(trace.spans),
                    sum(1 for s in trace.spans if s.status.code == StatusCode.ERROR),
                    started_ns,
                    seen_ns,
                ),
            )
        return inserted, updated, duplicates

    # -- span write helpers ----------------------------------------------

    def _insert_span(self, trace: Trace, span: Span, digest: str) -> None:
        self._conn.execute(
            """
            INSERT INTO spans (
                trace_id, project_id, span_id, parent_span_id, name, kind,
                started_ns, ended_ns, duration_ms,
                status_code, status_message,
                attributes_json, events_json, links_json,
                integrity_flags_json, provenance, span_hash
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                span.trace_id,
                trace.project_id,
                span.span_id,
                span.parent_span_id,
                span.name,
                span.kind.value,
                _ns(span.start_time),
                _ns(span.end_time),
                span.duration_ms,
                span.status.code.value,
                span.status.message,
                json.dumps(span.attributes, sort_keys=True, default=str),
                json.dumps(
                    [e.model_dump(mode="json") for e in span.events],
                    sort_keys=True, default=str,
                ),
                json.dumps(
                    [lk.model_dump(mode="json") for lk in span.links],
                    sort_keys=True, default=str,
                ),
                json.dumps([f.value for f in span.integrity_flags]),
                span.provenance.value,
                digest,
            ),
        )

    def _update_span(self, trace: Trace, merged: Span) -> None:
        self._conn.execute(
            """
            UPDATE spans SET
                parent_span_id=?, ended_ns=?, duration_ms=?,
                status_code=?, status_message=?,
                attributes_json=?, events_json=?, links_json=?,
                integrity_flags_json=?, provenance=?, span_hash=?
            WHERE project_id=? AND trace_id=? AND span_id=?
            """,
            (
                merged.parent_span_id,
                _ns(merged.end_time),
                merged.duration_ms,
                merged.status.code.value,
                merged.status.message,
                json.dumps(merged.attributes, sort_keys=True, default=str),
                json.dumps(
                    [e.model_dump(mode="json") for e in merged.events],
                    sort_keys=True, default=str,
                ),
                json.dumps(
                    [lk.model_dump(mode="json") for lk in merged.links],
                    sort_keys=True, default=str,
                ),
                json.dumps([f.value for f in merged.integrity_flags]),
                merged.provenance.value,
                _span_digest(merged),
                trace.project_id,
                merged.trace_id,
                merged.span_id,
            ),
        )

    @staticmethod
    def _identity_differs(stored: Span, incoming: Span) -> bool:
        """True when merge-untouched fields disagree between deliveries.

        ``name``/``kind``/``start_time`` are never merged (first-observed
        wins), so a difference means the same ``(project, trace, span)``
        key carried *different* telemetry — a conflict, not a retry.
        """
        return (
            incoming.name != stored.name
            or incoming.kind != stored.kind
            or incoming.start_time != stored.start_time
        )

    def _merge_span(
        self, stored: Span, incoming: Span, project_id: str
    ) -> Span | None:
        """Merge a re-delivered span into the stored row.

        Returns the merged ``Span`` when it adds information, ``None``
        when the stored row already covers everything (→ duplicate).
        Mutable fields merge; identity fields (name/kind/start) stay
        first-observed.
        """
        # end_time: the latest observation wins — an older end_time must
        # not regress a completed span (flagged OUT_OF_ORDER as evidence
        # of a late-arriving stale delivery)
        out_of_order = (
            stored.end_time is not None
            and incoming.end_time is not None
            and ensure_aware(incoming.end_time) < ensure_aware(stored.end_time)
        )
        merged = stored.model_copy(
            update={
                "parent_span_id": stored.parent_span_id or incoming.parent_span_id,
                "end_time": _max_dt(stored.end_time, incoming.end_time),
                "attributes": {**stored.attributes, **incoming.attributes},
                "events": _merge_events(stored.events, incoming.events),
                "links": _merge_links(stored.links, incoming.links),
                "status": _merge_status(stored.status, incoming.status),
                # same rule as the trace row: strongest provenance wins
                "provenance": _stronger_provenance(
                    stored.provenance, incoming.provenance
                ),
            }
        )
        # integrity flags: union minus flags resolved by the merged data
        flags = set(stored.integrity_flags) | set(incoming.integrity_flags)
        if out_of_order:
            flags.add(IntegrityFlag.OUT_OF_ORDER)
        if merged.end_time is not None:
            flags.discard(IntegrityFlag.MISSING_END)
        if merged.parent_span_id is None or self._span_exists(
            project_id, merged.trace_id, merged.parent_span_id
        ):
            flags.discard(IntegrityFlag.MISSING_PARENT)
        merged.integrity_flags = sorted(flags, key=lambda f: f.value)

        if merged.model_dump(mode="json", exclude={"integrity_flags"}) == stored.model_dump(
            mode="json", exclude={"integrity_flags"}
        ):
            return None
        return merged

    def _union_flags(
        self, row: sqlite3.Row, span: Span, *, conflict: bool = False
    ) -> None:
        """Union incoming integrity flags into the stored row.

        Every re-delivery leaves evidence: ``DUPLICATE`` for a replay,
        ``CONFLICT`` when the same key carried different telemetry
        (merge-untouched fields disagree). Flags the incoming span
        carried that we lack are still observations — recorded even when
        the payload itself changed nothing.
        """
        stored = set(json.loads(row["integrity_flags_json"] or "[]"))
        merged = stored | {f.value for f in span.integrity_flags} | {
            (IntegrityFlag.CONFLICT if conflict else IntegrityFlag.DUPLICATE).value
        }
        if merged != stored:
            self._conn.execute(
                "UPDATE spans SET integrity_flags_json=? "
                "WHERE project_id=? AND trace_id=? AND span_id=?",
                (
                    json.dumps(sorted(merged)),
                    row["project_id"],
                    row["trace_id"],
                    row["span_id"],
                ),
            )

    def _span_exists(self, project_id: str, trace_id: str, span_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM spans WHERE project_id=? AND trace_id=? AND span_id=?",
                (project_id, trace_id, span_id),
            ).fetchone()
            is not None
        )

    def _refresh_integrity_flags(self, trace: Trace) -> None:
        """Clear MISSING_PARENT/MISSING_END flags the stored data resolved."""
        rows = self._conn.execute(
            "SELECT span_id, parent_span_id, ended_ns, integrity_flags_json FROM spans "
            "WHERE project_id=? AND trace_id=? AND ("
            "  integrity_flags_json LIKE '%missing_parent%' OR"
            "  integrity_flags_json LIKE '%missing_end%')",
            (trace.project_id, trace.trace_id),
        ).fetchall()
        for r in rows:
            flags = json.loads(r["integrity_flags_json"])
            keep = list(flags)
            if r["ended_ns"] is not None:
                keep = [f for f in keep if f != IntegrityFlag.MISSING_END.value]
            if r["parent_span_id"] is None or self._span_exists(
                trace.project_id, trace.trace_id, r["parent_span_id"]
            ):
                keep = [f for f in keep if f != IntegrityFlag.MISSING_PARENT.value]
            if keep != flags:
                self._conn.execute(
                    "UPDATE spans SET integrity_flags_json=? "
                    "WHERE project_id=? AND trace_id=? AND span_id=?",
                    (json.dumps(keep), trace.project_id, trace.trace_id, r["span_id"]),
                )

    # -- read -----------------------------------------------------------

    def _span_from_row(self, row: sqlite3.Row) -> Span:
        events = [
            SpanEvent(
                name=e.get("name", ""),
                time=datetime.fromisoformat(e["time"]),
                attributes=e.get("attributes", {}),
            )
            for e in json.loads(row["events_json"] or "[]")
        ]
        links = [
            SpanLink(
                trace_id=lk.get("trace_id", ""),
                span_id=lk.get("span_id", ""),
                attributes=lk.get("attributes", {}),
            )
            for lk in json.loads(row["links_json"] or "[]")
        ]
        flags_raw = json.loads(row["integrity_flags_json"] or "[]")
        return Span(
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            name=row["name"],
            kind=row["kind"],
            start_time=from_unix_nano(row["started_ns"]),
            end_time=_dt(row["ended_ns"]),
            attributes=json.loads(row["attributes_json"] or "{}"),
            events=events,
            links=links,
            status=Status(
                code=StatusCode(row["status_code"]),
                message=row["status_message"],
            ),
            provenance=Provenance(row["provenance"]),
            integrity_flags=flags_raw,
        )

    def get_trace(self, trace_id: str, project_id: str | None = None) -> Trace | None:
        with self._lock:
            if project_id:
                trow = self._conn.execute(
                    "SELECT * FROM traces WHERE project_id=? AND trace_id=?",
                    (project_id, trace_id),
                ).fetchone()
            else:
                trow = self._conn.execute(
                    "SELECT * FROM traces WHERE trace_id=? ORDER BY last_seen_ns DESC LIMIT 1",
                    (trace_id,),
                ).fetchone()
            if trow is None:
                return None
            srows = self._conn.execute(
                "SELECT * FROM spans WHERE project_id=? AND trace_id=? ORDER BY started_ns",
                (trow["project_id"], trace_id),
            ).fetchall()
        spans = [self._span_from_row(r) for r in srows]
        return Trace(
            trace_id=trace_id,
            project_id=trow["project_id"],
            session_id=trow["session_id"],
            root_span_id=trow["root_span_id"],
            started_at=from_unix_nano(trow["started_ns"]),
            ended_at=_dt(trow["ended_ns"]),
            status=Status(
                code=StatusCode(trow["status_code"]),
                message=trow["status_message"],
            ),
            source=TraceSource.model_validate_json(trow["source_json"]),
            resource_attributes=json.loads(trow["resource_attributes_json"] or "{}"),
            spans=spans,
            provenance=Provenance(trow["provenance"]),
        )

    def list_traces(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        clauses, params = [], []
        if project_id:
            clauses.append("project_id = ?")
            params.append(project_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status:
            clauses.append("status_code = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT trace_id, project_id, session_id, root_span_id,
                           started_ns, ended_ns, status_code, span_count, error_count
                    FROM traces {where}
                    ORDER BY started_ns DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
        return [
            {
                "trace_id": r["trace_id"],
                "project_id": r["project_id"],
                "session_id": r["session_id"],
                "root_span_id": r["root_span_id"],
                "started_at": (s.isoformat() if (s := _dt(r["started_ns"])) else None),
                "ended_at": (e.isoformat() if (e := _dt(r["ended_ns"])) else None),
                "status": r["status_code"],
                "span_count": r["span_count"],
                "error_count": r["error_count"],
            }
            for r in rows
        ]

    def stats(self) -> dict[str, Any]:
        with self._lock:
            traces = self._conn.execute("SELECT COUNT(*) c FROM traces").fetchone()["c"]
            spans = self._conn.execute("SELECT COUNT(*) c FROM spans").fetchone()["c"]
            errors = self._conn.execute(
                "SELECT COUNT(*) c FROM spans WHERE status_code='error'"
            ).fetchone()["c"]
            # WAL mode: live data also sits in -wal/-shm sidecars.
            db_size = sum(
                p.stat().st_size
                for p in (
                    self.path,
                    self.path.with_name(self.path.name + "-wal"),
                    self.path.with_name(self.path.name + "-shm"),
                )
                if p.exists()
            )
            schema = self._conn.execute(
                "SELECT value FROM lens_meta WHERE key='schema_version'"
            ).fetchone()
        return {
            "traces": traces,
            "spans": spans,
            "error_spans": errors,
            "db_size_bytes": db_size,
            "schema_version": schema["value"] if schema else None,
        }

    def close(self) -> None:
        with self._lock:
            self._conn.close()
