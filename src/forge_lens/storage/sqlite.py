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
import math
import sqlite3
import threading
from collections.abc import Iterable
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
from forge_lens.core.errors import ConflictError, ValidationError
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

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    name TEXT NOT NULL,
    task TEXT,
    created_ns INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, dataset_id)
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    dataset_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    version TEXT NOT NULL,
    description TEXT,
    created_ns INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, dataset_id, version)
);

CREATE TABLE IF NOT EXISTS dataset_cases (
    dataset_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    version TEXT NOT NULL,
    case_id TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    expected_json TEXT NOT NULL DEFAULT '{}',
    allowed_tools_json TEXT NOT NULL DEFAULT '[]',
    constraints_json TEXT NOT NULL DEFAULT '{}',
    rubric_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, dataset_id, version, case_id)
);

CREATE TABLE IF NOT EXISTS experiments (
    experiment_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    name TEXT,
    description TEXT,
    dataset_id TEXT,
    dataset_version TEXT,
    cohorts_json TEXT NOT NULL DEFAULT '{}',
    evaluators_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'open',
    result_ref TEXT,
    provenance TEXT NOT NULL DEFAULT 'imported',
    created_ns INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (project_id, experiment_id)
);

CREATE TABLE IF NOT EXISTS experiment_results (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    project_id TEXT NOT NULL DEFAULT 'default',
    run_ns INTEGER,
    report_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_experiment_results_exp
    ON experiment_results(project_id, experiment_id, run_id);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id TEXT NOT NULL,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    role TEXT NOT NULL,
    projects_json TEXT NOT NULL DEFAULT '[]',
    created_ns INTEGER,
    revoked_ns INTEGER,
    PRIMARY KEY (key_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ns INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts_ns);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_events(action);
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


def _dump_with_raw_attrs(
    model: Span | SpanEvent | SpanLink, *, exclude: set[str] | None = None
) -> dict[str, Any]:
    """``model_dump`` with the Any-typed ``attributes`` payload spliced
    back raw.

    Deep ``attributes`` trees go through pydantic-core's serializer when
    included — its recursion budget burns against ``sys.getrecursionlimit``
    headroom, which differs across builds (pytest/coverage consume frames).
    Splicing the raw dict back keeps the serialized content identical while
    the deep walk happens once in the C ``json`` encoder — the same on
    every platform.
    """
    excl = {"attributes"} | (exclude or set())
    dump = model.model_dump(mode="json", exclude=excl)
    dump["attributes"] = model.attributes
    return dump


def _span_comparable_dump(span: Span) -> dict[str, Any]:
    """Full span dump for digest/equality — deep attrs, events and links
    all bypass the pydantic-core recursion budget."""
    dump = _dump_with_raw_attrs(
        span, exclude={"integrity_flags", "events", "links"}
    )
    dump["events"] = [_dump_with_raw_attrs(e) for e in span.events]
    dump["links"] = [_dump_with_raw_attrs(lk) for lk in span.links]
    return dump


def _span_digest(span: Span) -> str:
    """Content hash of a span, excluding integrity flags (they evolve)."""
    payload = json.dumps(
        _json_finite(_span_comparable_dump(span)),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _json_finite(value: Any) -> Any:
    """Coerce non-finite floats to ``None`` before persistence.

    ``NaN``/``Infinity`` have no valid JSON literal — ``json.dumps``
    emits them bare and sqlite then stores ``json_valid()=0`` rows;
    ``json_patch`` silently mutates them to ``null`` on merge. ``None``
    is the honest representation of an unrepresentable number.
    """
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _json_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_finite(v) for v in value]
    return value


_INT64_MAX = 2**63 - 1


def _check_i64(name: str, value: int) -> int:
    """SQLite binds raise ``OverflowError`` outside int64 — validate at
    the store boundary so every caller (API/CLI/MCP) gets a loud,
    typed failure instead of a driver error."""
    if value > _INT64_MAX or value < -_INT64_MAX - 1:
        raise ValueError(f"{name}: out of int64 range")
    return value


def _utf8_ok(value: str | None) -> bool:
    """True when the driver can bind ``value`` as TEXT.

    Lone surrogates (argv surrogateescape, WTF-8 request bodies) raise
    ``UnicodeEncodeError`` inside sqlite3 at bind time. An unencodable
    string can never equal a stored one, so *reads* treat it as absent
    and *writes* reject it as ``ValidationError`` — never a raw driver
    error."""
    if value is None:
        return True
    try:
        value.encode("utf-8")
        return True
    except UnicodeEncodeError:
        return False


def _require_utf8(bound: dict[str, str | None]) -> None:
    """Reject TEXT binds the driver can't encode — store-level contract:
    a lone surrogate is a ``ValidationError``, not a UnicodeEncodeError
    surfacing mid-write."""
    bad = [name for name, value in bound.items() if not _utf8_ok(value)]
    if bad:
        raise ValidationError(
            f"unencodable string (lone surrogate) in: {', '.join(sorted(bad))}"
        )


def _like_escape(value: str) -> str:
    """Literal-ize LIKE metacharacters in a user-supplied substring.

    ``\\`` first — escaping ``%``/``_`` before the escape char would
    double-escape the markers themselves. Bound with ``ESCAPE '\\'``
    so a ``%`` in a search term matches a literal percent sign, never
    acts as a wildcard.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _derived_status_code(spans: list[Span]) -> str | None:
    """The UPDATE path's CASE, in Python — bound on INSERT so a first
    write and its identical re-delivery agree (idempotent status)."""
    if not spans:
        return None
    if any(s.status.code == StatusCode.ERROR for s in spans):
        return "error"
    if all(s.status.code == StatusCode.OK for s in spans):
        return "ok"
    return "unset"


def _events_json(events: list[SpanEvent]) -> str:
    dumped = [
        _dump_with_raw_attrs(e.model_copy(update={"time": ensure_aware(e.time)}))
        for e in events
    ]
    return json.dumps(_json_finite(dumped), sort_keys=True, default=str)


def _links_json(links: list[SpanLink]) -> str:
    dumped = [_dump_with_raw_attrs(lk) for lk in links]
    return json.dumps(_json_finite(dumped), sort_keys=True, default=str)


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

    def __init__(self, path: Path | str, *, readonly: bool = False) -> None:
        # expanduser: sqlite never sees a "~" — `--db ~/x.db` used to
        # create a literal "./~" directory. resolve: a relative path must
        # stay bound to the file it opened — stats() sizes by name and
        # would silently report 0 after the caller's cwd moved on.
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        if readonly:
            # probe mode (e.g. doctor): never create dirs, never run
            # init_schema or PRAGMA journal_mode — a health check must
            # not mutate the database it inspects. as_uri() percent-
            # encodes: a literal '?' or '%' in the path would be parsed
            # as the URI query and silently open a different file.
            # A plain mode=ro open on a cold WAL db still *creates*
            # -shm/-wal sidecars — ghost files a probe must not leave.
            # With no -wal every byte lives in the checkpointed main
            # file, so immutable=1 loses nothing; when a -wal does exist
            # (live or crash-leftover) it may carry real data and plain
            # ro reads it, recreating -shm only if the dir allows.
            wal = self.path.with_name(self.path.name + "-wal")
            shm = self.path.with_name(self.path.name + "-shm")
            # SQLite permits immutable=1 only when BOTH sidecars are
            # absent — a lone -shm means a WAL-mode peer is/was live.
            # Checking both shrinks (not closes) the TOCTOU window: a
            # -wal appearing between the exists() check and the open
            # leaves an immutable view over live WAL data, which fails
            # on first read (SQLITE_CORRUPT) — so the open is verified
            # with a schema probe and falls back to plain ro, where
            # busy_timeout arbitrates the live peer.
            immutable = not (wal.exists() or shm.exists())
            uri = self.path.as_uri() + (
                "?mode=ro&immutable=1" if immutable else "?mode=ro"
            )
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                conn.execute("PRAGMA schema_version").fetchone()
            except sqlite3.Error:
                if conn is not None:
                    conn.close()
                if not immutable:
                    raise
                conn = sqlite3.connect(
                    self.path.as_uri() + "?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )
            self._conn = conn
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Default (deferred) isolation level so `with self._conn:` is a
            # real transaction — upsert_trace is atomic per
            # docs/architecture/storage.md.
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")
        if not readonly:
            self._conn.execute("PRAGMA journal_mode=WAL")
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
        # Bound TEXT must be UTF-8-encodable — a lone surrogate (argv
        # surrogateescape, WTF-8 producers) reaches the driver as
        # UnicodeEncodeError otherwise. The pipeline screens its own
        # path (_ensure_storable); this is the same guarantee for direct
        # callers. Attributes/events/links ride json.dumps escapes, so
        # only fields bound raw are listed.
        bound: dict[str, str | None] = {
            "trace_id": trace.trace_id,
            "project_id": trace.project_id,
            "session_id": trace.session_id,
            "root_span_id": trace.root_span_id,
            "status.message": trace.status.message,
        }
        for i, span in enumerate(trace.spans):
            bound[f"spans[{i}].trace_id"] = span.trace_id
            bound[f"spans[{i}].span_id"] = span.span_id
            bound[f"spans[{i}].parent_span_id"] = span.parent_span_id
            bound[f"spans[{i}].name"] = span.name
            bound[f"spans[{i}].status.message"] = span.status.message
        _require_utf8(bound)
        inserted = updated = duplicates = 0
        # Deterministic seen-markers derived from trace data — no wall clock.
        # ``started_at`` is required by the contract, so it is never None.
        started_ns = to_unix_nano(trace.started_at)
        ended_ns = _ns(trace.ended_at)
        seen_ns = max(started_ns, ended_ns) if ended_ns is not None else started_ns
        with self._lock, self._conn:
            # One prefetch stands in for the per-span PK SELECT: the
            # dedup/merge checks below only need the stored row per
            # span_id, and this snapshot is live state — it shares the
            # transaction the loop writes in, so nothing mutates it
            # underneath us. O(1) queries per trace instead of O(spans).
            prefetched: dict[str, sqlite3.Row] = {
                r["span_id"]: r
                for r in self._conn.execute(
                    "SELECT * FROM spans WHERE project_id=? AND trace_id=?",
                    (trace.project_id, trace.trace_id),
                ).fetchall()
            }
            delivered: set[str] = set()
            for span in trace.spans:
                if span.span_id in delivered:
                    # a second occurrence of a span_id inside one trace
                    # must see the row its predecessor just wrote —
                    # the prefetch predates it, so re-read live (exactly
                    # the row the old per-span SELECT would have seen)
                    row = self._conn.execute(
                        "SELECT * FROM spans WHERE project_id=? AND trace_id=? "
                        "AND span_id=?",
                        (trace.project_id, span.trace_id, span.span_id),
                    ).fetchone()
                else:
                    row = prefetched.get(span.span_id)
                delivered.add(span.span_id)
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
                    _derived_status_code(trace.spans) or trace.status.code.value,
                    trace.status.message,
                    trace.source.model_dump_json(),
                    json.dumps(
                        _json_finite(trace.resource_attributes),
                        sort_keys=True, default=str,
                    ),
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
                json.dumps(_json_finite(span.attributes), sort_keys=True, default=str),
                _events_json(span.events),
                _links_json(span.links),
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
                json.dumps(_json_finite(merged.attributes), sort_keys=True, default=str),
                _events_json(merged.events),
                _links_json(merged.links),
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
        ``parent_span_id`` merges first-non-None, so a *different*
        declared parent is a topology conflict — the merge would drop it
        silently otherwise.
        """
        return (
            incoming.name != stored.name
            or incoming.kind != stored.kind
            or incoming.start_time != stored.start_time
            or (
                incoming.parent_span_id is not None
                and stored.parent_span_id is not None
                and incoming.parent_span_id != stored.parent_span_id
            )
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
            # parent resolved inside this trace — both absence flags die
            flags.discard(IntegrityFlag.MISSING_PARENT)
            flags.discard(IntegrityFlag.ORPHAN)
        merged.integrity_flags = sorted(flags, key=lambda f: f.value)

        if _span_comparable_dump(merged) == _span_comparable_dump(stored):
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

    def span_trace_id(
        self, project_id: str, span_id: str, *, trace_id: str | None = None
    ) -> str | None:
        """Which stored trace owns ``span_id`` in this project, if any.

        Used at ingest to mark ORPHAN: a ``parent_span_id`` that exists
        under a *different* trace will never resolve inside this one.
        A ``span_id`` can legitimately live under several traces
        (cross-trace deliveries); the same-trace row is preferred so a
        parent already delivered *here* doesn't produce a false ORPHAN,
        and remaining ties break on ``trace_id`` for determinism.
        """
        if not all(_utf8_ok(v) for v in (project_id, span_id, trace_id)):
            return None
        row = self._conn.execute(
            "SELECT trace_id FROM spans WHERE project_id=? AND span_id=? "
            "ORDER BY (trace_id = ?) DESC, trace_id LIMIT 1",
            (project_id, span_id, trace_id or ""),
        ).fetchone()
        return str(row["trace_id"]) if row else None

    def _refresh_integrity_flags(self, trace: Trace) -> None:
        """Clear MISSING_PARENT/ORPHAN/MISSING_END the stored data resolved."""
        # json_each gives an exact element match — LIKE '%missing_end%'
        # would treat '_' as a single-char wildcard
        rows = self._conn.execute(
            "SELECT span_id, parent_span_id, ended_ns, integrity_flags_json FROM spans "
            "WHERE project_id=? AND trace_id=? AND EXISTS ("
            "  SELECT 1 FROM json_each(spans.integrity_flags_json) je"
            "  WHERE je.value IN ('missing_parent','missing_end','orphan'))",
            (trace.project_id, trace.trace_id),
        ).fetchall()
        resolved_parent = {
            IntegrityFlag.MISSING_PARENT.value,
            IntegrityFlag.ORPHAN.value,
        }
        for r in rows:
            flags = json.loads(r["integrity_flags_json"])
            keep = list(flags)
            if r["ended_ns"] is not None:
                keep = [f for f in keep if f != IntegrityFlag.MISSING_END.value]
            if r["parent_span_id"] is None or self._span_exists(
                trace.project_id, trace.trace_id, r["parent_span_id"]
            ):
                # the parent now exists *in this trace* — orphanhood is
                # stale the same way missing_parent is
                keep = [f for f in keep if f not in resolved_parent]
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

    def _trace_from_rows(self, trow: sqlite3.Row, srows: list[sqlite3.Row]) -> Trace:
        """Hydrate a Trace from its traces row + ordered span rows —
        the one hydration path shared by ``get_trace``/``get_traces``."""
        return Trace(
            trace_id=trow["trace_id"],
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
            spans=[self._span_from_row(r) for r in srows],
            provenance=Provenance(trow["provenance"]),
        )

    def get_trace(self, trace_id: str, project_id: str | None = None) -> Trace | None:
        # a str carrying lone surrogates (argv surrogateescape, WTF-8
        # request bodies) raises UnicodeEncodeError in the driver — an
        # unencodable id can never match a stored one, so it is "absent"
        if not _utf8_ok(trace_id) or not _utf8_ok(project_id):
            return None
        with self._lock:
            if project_id is not None:
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
                "SELECT * FROM spans WHERE project_id=? AND trace_id=? "
                "ORDER BY started_ns, span_id",
                (trow["project_id"], trace_id),
            ).fetchall()
        return self._trace_from_rows(trow, srows)

    # SQLite's SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds —
    # a 500-id chunk stays under it with headroom for extra binds.
    _BATCH_CHUNK = 500

    def get_traces(
        self, project_id: str | None, trace_ids: Iterable[str]
    ) -> dict[str, Trace | None]:
        """Batch ``get_trace``: one ``traces`` query plus one grouped
        ``spans`` query per ≤500-id chunk, all under a single lock
        acquisition — the cohort/window endpoints' N+1 killer.

        Duplicate ids collapse into one entry; callers keep their own
        input list when per-id order/duplicates matter. Absent (or
        unencodable) ids map to ``None`` — hydration is identical to
        ``get_trace`` and missing data stays missing.
        """
        out: dict[str, Trace | None] = {}
        ids: list[str] = []
        for tid in trace_ids:
            if tid not in out:
                out[tid] = None
                # an unencodable id stays out of the IN list — the same
                # "absent" verdict get_trace gives lone surrogates
                if _utf8_ok(tid):
                    ids.append(tid)
        if not ids or not _utf8_ok(project_id):
            return out
        with self._lock:
            for lo in range(0, len(ids), self._BATCH_CHUNK):
                chunk = ids[lo : lo + self._BATCH_CHUNK]
                ph = ", ".join("?" for _ in chunk)
                if project_id is not None:
                    trows = self._conn.execute(
                        "SELECT * FROM traces WHERE project_id=? "
                        f"AND trace_id IN ({ph})",
                        (project_id, *chunk),
                    ).fetchall()
                else:
                    # wildcard — ORDER BY last_seen_ns DESC so the first
                    # row per trace_id wins, mirroring get_trace's LIMIT 1
                    trows = self._conn.execute(
                        f"SELECT * FROM traces WHERE trace_id IN ({ph}) "
                        "ORDER BY last_seen_ns DESC",
                        tuple(chunk),
                    ).fetchall()
                winners: dict[str, sqlite3.Row] = {}
                for r in trows:
                    cur = winners.get(r["trace_id"])
                    if cur is None or r["last_seen_ns"] > cur["last_seen_ns"]:
                        winners[r["trace_id"]] = r
                if not winners:
                    continue
                # spans hydrate per winning (project_id, trace_id):
                # grouped by project so a wildcard lookup never mixes
                # tenants — same-trace-id rows under different projects
                # stay independent
                by_pid: dict[str, list[str]] = {}
                for w in winners.values():
                    by_pid.setdefault(w["project_id"], []).append(w["trace_id"])
                spans_by_tid: dict[str, list[sqlite3.Row]] = {}
                for pid, want in by_pid.items():
                    ph2 = ", ".join("?" for _ in want)
                    for r in self._conn.execute(
                        f"SELECT * FROM spans WHERE project_id=? AND trace_id "
                        f"IN ({ph2}) ORDER BY started_ns, span_id",
                        (pid, *want),
                    ).fetchall():
                        spans_by_tid.setdefault(r["trace_id"], []).append(r)
                for tid, trow in winners.items():
                    out[tid] = self._trace_from_rows(
                        trow, spans_by_tid.get(tid, [])
                    )
        return out

    def list_traces(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        since_ns: int | None = None,
        until_ns: int | None = None,
        min_duration_ns: int | None = None,
        max_duration_ns: int | None = None,
        q: str | None = None,
        tool: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        host: str | None = None,
        min_total_tokens: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Filtered, paginated trace listing — every predicate ANDs.

        ``since_ns``/``until_ns`` bound ``started_ns`` inclusively.
        ``min_duration_ns``/``max_duration_ns`` compare against
        ``ended_ns - started_ns``: a trace whose ``ended_ns`` is NULL
        has no observable duration, so *any* duration filter excludes
        it (never coerced to 0 or "still running").
        ``q`` is a literal substring matched against ``trace_id`` OR
        any span ``name``; ``tool`` matches traces carrying a span
        whose ``tool.name`` attribute contains the value. Both are
        LIKE-escaped — user input can never inject wildcards.
        ``model`` matches ``gen_ai.request.model``/``gen_ai.response.model``
        span attrs; ``agent`` matches ``gen_ai.agent.name`` on spans OR
        ``source.system``/``service.name`` producer identity; ``host``
        matches ``source.host``/``host.name`` resource attrs.
        ``min_total_tokens`` compares the summed
        ``gen_ai.usage.total_tokens`` across a trace's spans — a trace
        needs at least one span OBSERVING usage to match at all (no
        evidence ≠ observed zero, even at bound 0). ``cost`` is
        deliberately NOT a filter: it derives from the pricing registry
        at read time and isn't a stored column — post-computing it over
        unbounded sets would be dishonest about scope.
        ``project_id=None`` stays the all-projects wildcard.
        All int binds are int64-checked — an out-of-range value is a
        ``ValueError``, never the driver's ``OverflowError``.
        """
        _check_i64("limit", limit)
        _check_i64("offset", offset)
        for name, v in (
            ("since_ns", since_ns),
            ("until_ns", until_ns),
            ("min_duration_ns", min_duration_ns),
            ("max_duration_ns", max_duration_ns),
        ):
            if v is not None:
                _check_i64(name, v)
        if min_total_tokens is not None:
            if min_total_tokens < 0:
                raise ValueError("min_total_tokens: non-negative required")
            _check_i64("min_total_tokens", min_total_tokens)
        # an unencodable filter can never match stored TEXT → empty page,
        # never a UnicodeEncodeError from the driver
        if not all(
            _utf8_ok(v)
            for v in (project_id, session_id, status, q, tool, model, agent, host)
        ):
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status:
            clauses.append("status_code = ?")
            params.append(status)
        if since_ns is not None:
            clauses.append("started_ns >= ?")
            params.append(since_ns)
        if until_ns is not None:
            clauses.append("started_ns <= ?")
            params.append(until_ns)
        if min_duration_ns is not None or max_duration_ns is not None:
            # NULL ended_ns = no duration evidence — excluded on purpose
            clauses.append("ended_ns IS NOT NULL")
            if min_duration_ns is not None:
                clauses.append("(ended_ns - started_ns) >= ?")
                params.append(min_duration_ns)
            if max_duration_ns is not None:
                clauses.append("(ended_ns - started_ns) <= ?")
                params.append(max_duration_ns)
        if q:
            clauses.append(
                "(trace_id LIKE ? ESCAPE '\\' OR EXISTS ("
                "SELECT 1 FROM spans s "
                "WHERE s.project_id = traces.project_id "
                "AND s.trace_id = traces.trace_id "
                "AND s.name LIKE ? ESCAPE '\\'))"
            )
            pat = f"%{_like_escape(q)}%"
            params.extend([pat, pat])
        if tool:
            # Field-scoped: the substring must live inside the
            # `tool.name` VALUE — a JSON-fragment LIKE would let pat
            # match text in any later key of the same blob.
            clauses.append(
                "EXISTS (SELECT 1 FROM spans s, "
                "json_each(s.attributes_json) j "
                "WHERE s.project_id = traces.project_id "
                "AND s.trace_id = traces.trace_id "
                "AND j.key = 'tool.name' "
                "AND j.value LIKE ? ESCAPE '\\')"
            )
            params.append(f"%{_like_escape(tool)}%")
        if model:
            pat = _like_escape(model)
            clauses.append(
                "EXISTS (SELECT 1 FROM spans s, "
                "json_each(s.attributes_json) j "
                "WHERE s.project_id = traces.project_id "
                "AND s.trace_id = traces.trace_id "
                "AND j.key IN ('gen_ai.request.model', "
                "'gen_ai.response.model') "
                "AND j.value LIKE ? ESCAPE '\\')"
            )
            params.append(f"%{pat}%")
        if agent:
            pat = _like_escape(agent)
            clauses.append(
                "(EXISTS (SELECT 1 FROM spans s, "
                "json_each(s.attributes_json) j "
                "WHERE s.project_id = traces.project_id "
                "AND s.trace_id = traces.trace_id "
                "AND j.key = 'gen_ai.agent.name' "
                "AND j.value LIKE ? ESCAPE '\\') "
                "OR json_extract(traces.source_json, '$.system') "
                "LIKE ? ESCAPE '\\' "
                "OR json_extract(traces.resource_attributes_json, "
                "'$.\"service.name\"') LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{pat}%", f"%{pat}%", f"%{pat}%"])
        if host:
            pat = _like_escape(host)
            clauses.append(
                "(json_extract(traces.source_json, '$.host') "
                "LIKE ? ESCAPE '\\' "
                "OR json_extract(traces.resource_attributes_json, "
                "'$.\"host.name\"') LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{pat}%", f"%{pat}%"])
        if min_total_tokens is not None:
            # >= bound AND at least one span must actually observe
            # usage — traces with no token evidence never match, even
            # at bound 0.
            clauses.append(
                "(EXISTS (SELECT 1 FROM spans s "
                "WHERE s.project_id = traces.project_id "
                "AND s.trace_id = traces.trace_id "
                "AND json_extract(s.attributes_json, "
                "'$.\"gen_ai.usage.total_tokens\"') IS NOT NULL) "
                "AND (SELECT COALESCE(SUM(json_extract(s.attributes_json, "
                "'$.\"gen_ai.usage.total_tokens\"')), 0) FROM spans s "
                "WHERE s.project_id = traces.project_id "
                "AND s.trace_id = traces.trace_id) >= ?)"
            )
            params.append(min_total_tokens)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT trace_id, project_id, session_id, root_span_id,
                           started_ns, ended_ns, status_code, span_count, error_count
                    FROM traces {where}
                    ORDER BY started_ns DESC, project_id, trace_id
                    LIMIT ? OFFSET ?""",
                (*params, max(0, limit), max(0, offset)),
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

    def list_sessions(
        self,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """One row per observed ``session_id``.

        ``first_started_at`` = earliest trace start in the session;
        ``last_activity_at`` = latest observable moment — a completed
        trace's ``ended_at``, a still-running trace's ``started_at``
        (scalar ``MAX`` propagates NULL, so the COALESCE is load-bearing).
        Order is deterministic: most-recent activity first, ties break
        on ``session_id`` ASC. ``project_id=None`` is the same
        all-projects wildcard as ``list_traces``.
        """
        _check_i64("limit", limit)
        _check_i64("offset", offset)
        if not _utf8_ok(project_id):
            return []
        clauses = ["session_id IS NOT NULL"]
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT session_id,
                           COUNT(*) AS trace_count,
                           SUM(span_count) AS span_total,
                           MIN(started_ns) AS first_started_ns,
                           MAX(MAX(started_ns, COALESCE(ended_ns, started_ns)))
                               AS last_activity_ns
                    FROM traces WHERE {' AND '.join(clauses)}
                    GROUP BY session_id
                    ORDER BY last_activity_ns DESC, session_id ASC
                    LIMIT ? OFFSET ?""",
                (*params, max(0, limit), max(0, offset)),
            ).fetchall()
        return [
            {
                "session_id": r["session_id"],
                "traces": r["trace_count"],
                "spans": r["span_total"] or 0,
                "first_started_at": (
                    d.isoformat() if (d := _dt(r["first_started_ns"])) else None
                ),
                "last_activity_at": (
                    d.isoformat() if (d := _dt(r["last_activity_ns"])) else None
                ),
            }
            for r in rows
        ]

    def list_tools(
        self,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """Fleet-wide roll-up over ``kind='tool'`` spans — one row per
        ``tool.name`` attribute value. A TOOL span without the attribute
        rolls into ``(unnamed)`` rather than vanishing, so counts always
        reconcile with the span table. ``project_id=None`` is the
        all-projects wildcard. Deterministic order: busiest first,
        ties on name ASC."""
        _check_i64("limit", limit)
        _check_i64("offset", offset)
        if not _utf8_ok(project_id):
            return []
        clauses = ["kind = 'tool'"]
        params: list[Any] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT json_extract(attributes_json, '$."tool.name"')
                           AS tool_name,
                           COUNT(*) AS calls,
                           SUM(CASE WHEN status_code = 'error'
                                    THEN 1 ELSE 0 END) AS errors,
                           AVG(duration_ms) AS avg_ms,
                           COUNT(DISTINCT trace_id) AS trace_count
                    FROM spans WHERE {' AND '.join(clauses)}
                    GROUP BY tool_name
                    ORDER BY calls DESC, tool_name ASC
                    LIMIT ? OFFSET ?""",
                (*params, max(0, limit), max(0, offset)),
            ).fetchall()
        return [
            {
                "tool": r["tool_name"] if r["tool_name"] is not None
                else "(unnamed)",
                "calls": r["calls"],
                "traces": r["trace_count"],
                "errors": r["errors"],
                "avg_duration_ms": r["avg_ms"],
            }
            for r in rows
        ]

    def stats(self, project_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if project_id is not None and not _utf8_ok(project_id):
                # unencodable id can never match a stored project — the
                # counts are honestly zero; size/schema still computed
                traces = spans = errors = 0
            else:
                where = " WHERE project_id = ?" if project_id is not None else ""
                params = (project_id,) if project_id is not None else ()
                traces = self._conn.execute(
                    f"SELECT COUNT(*) c FROM traces{where}", params
                ).fetchone()["c"]
                spans = self._conn.execute(
                    f"SELECT COUNT(*) c FROM spans{where}", params
                ).fetchone()["c"]
                errors = self._conn.execute(
                    "SELECT COUNT(*) c FROM spans WHERE status_code='error'"
                    + (" AND project_id = ?" if project_id is not None else ""),
                    params if project_id is not None else (),
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
    ) -> None:
        """Insert or update dataset metadata (name/task/metadata).

        Datasets themselves are mutable metadata; their *versions* are
        append-only (see ``upsert_dataset_version``)."""
        _require_utf8(
            {
                "dataset_id": dataset_id,
                "project_id": project_id,
                "name": name,
                "task": task,
            }
        )
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO datasets(dataset_id, project_id, name, task,
                                        created_ns, metadata_json)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(project_id, dataset_id) DO UPDATE SET
                       name=excluded.name, task=excluded.task,
                       metadata_json=excluded.metadata_json""",
                (
                    dataset_id,
                    project_id,
                    name,
                    task,
                    now_ns,
                    json.dumps(_json_finite(metadata or {}),
                               sort_keys=True, default=str),
                ),
            )

    def get_dataset(
        self, dataset_id: str, project_id: str = "default"
    ) -> dict[str, Any] | None:
        if not _utf8_ok(dataset_id) or not _utf8_ok(project_id):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM datasets WHERE project_id=? AND dataset_id=?",
                (project_id, dataset_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "dataset_id": row["dataset_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "task": row["task"],
            "created_at": (d.isoformat() if (d := _dt(row["created_ns"])) else None),
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def list_datasets(self, project_id: str | None = None) -> list[dict[str, Any]]:
        if not _utf8_ok(project_id):
            return []
        with self._lock:
            if project_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM datasets WHERE project_id=? "
                    "ORDER BY dataset_id",
                    (project_id,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM datasets ORDER BY project_id, dataset_id"
                ).fetchall()
        return [
            {
                "dataset_id": r["dataset_id"],
                "project_id": r["project_id"],
                "name": r["name"],
                "task": r["task"],
                "created_at": (
                    d.isoformat() if (d := _dt(r["created_ns"])) else None
                ),
            }
            for r in rows
        ]

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
    ) -> int:
        """Store a dataset version and its cases atomically.

        Versions are append-only: the insert is ``ON CONFLICT DO
        NOTHING`` and a pre-existing ``(project, dataset, version)``
        raises :class:`ConflictError` inside the same transaction — a
        racing PUT can never slip past a check-then-act gap to mutate
        an already-referenced version."""
        bound: dict[str, str | None] = {
            "dataset_id": dataset_id,
            "project_id": project_id,
            "version": version,
            "description": description,
        }
        for i, c in enumerate(cases):
            bound[f"cases[{i}].case_id"] = str(c.get("case_id", ""))
        _require_utf8(bound)
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT INTO dataset_versions(dataset_id, project_id, version,
                                                description, created_ns, metadata_json)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(project_id, dataset_id, version) DO NOTHING""",
                (
                    dataset_id,
                    project_id,
                    version,
                    description,
                    now_ns,
                    json.dumps(_json_finite(metadata or {}),
                               sort_keys=True, default=str),
                ),
            )
            if cur.rowcount == 0:
                raise ConflictError(
                    f"dataset version {dataset_id!r}@{version!r} already "
                    f"exists in project {project_id!r}"
                )
            for c in cases:
                self._conn.execute(
                    """INSERT INTO dataset_cases(dataset_id, project_id, version,
                       case_id, input_json, expected_json, allowed_tools_json,
                       constraints_json, rubric_json)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        dataset_id,
                        project_id,
                        version,
                        str(c.get("case_id", "")),
                        json.dumps(_json_finite(c.get("input") or {}),
                                   sort_keys=True, default=str),
                        json.dumps(_json_finite(c.get("expected") or {}),
                                   sort_keys=True, default=str),
                        json.dumps(_json_finite(c.get("allowed_tools") or []),
                                   default=str),
                        json.dumps(_json_finite(c.get("constraints") or {}),
                                   sort_keys=True, default=str),
                        json.dumps(_json_finite(c.get("rubric") or {}),
                                   sort_keys=True, default=str),
                    ),
                )
        return len(cases)

    def get_dataset_version(
        self,
        dataset_id: str,
        version: str,
        project_id: str = "default",
    ) -> dict[str, Any] | None:
        if not all(_utf8_ok(v) for v in (dataset_id, version, project_id)):
            return None
        with self._lock:
            vrow = self._conn.execute(
                "SELECT * FROM dataset_versions WHERE project_id=? "
                "AND dataset_id=? AND version=?",
                (project_id, dataset_id, version),
            ).fetchone()
            if vrow is None:
                return None
            crows = self._conn.execute(
                "SELECT * FROM dataset_cases WHERE project_id=? AND dataset_id=? "
                "AND version=? ORDER BY case_id",
                (project_id, dataset_id, version),
            ).fetchall()
        return {
            "dataset_id": vrow["dataset_id"],
            "project_id": vrow["project_id"],
            "version": vrow["version"],
            "description": vrow["description"],
            "created_at": (
                d.isoformat() if (d := _dt(vrow["created_ns"])) else None
            ),
            "metadata": json.loads(vrow["metadata_json"] or "{}"),
            "cases": [
                {
                    "case_id": c["case_id"],
                    "input": json.loads(c["input_json"] or "{}"),
                    "expected": json.loads(c["expected_json"] or "{}"),
                    "allowed_tools": json.loads(c["allowed_tools_json"] or "[]"),
                    "constraints": json.loads(c["constraints_json"] or "{}"),
                    "rubric": json.loads(c["rubric_json"] or "{}"),
                }
                for c in crows
            ],
        }

    def list_dataset_versions(
        self, dataset_id: str, project_id: str = "default"
    ) -> list[dict[str, Any]]:
        if not _utf8_ok(dataset_id) or not _utf8_ok(project_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                """SELECT v.*, COUNT(c.case_id) AS case_count
                   FROM dataset_versions v
                   LEFT JOIN dataset_cases c
                     ON c.project_id=v.project_id AND c.dataset_id=v.dataset_id
                    AND c.version=v.version
                   WHERE v.project_id=? AND v.dataset_id=?
                   GROUP BY v.version ORDER BY v.version""",
                (project_id, dataset_id),
            ).fetchall()
        return [
            {
                "dataset_id": r["dataset_id"],
                "project_id": r["project_id"],
                "version": r["version"],
                "description": r["description"],
                "case_count": r["case_count"],
                "created_at": (
                    d.isoformat() if (d := _dt(r["created_ns"])) else None
                ),
            }
            for r in rows
        ]

    # -- experiments (Cycle 13) ------------------------------------------

    @staticmethod
    def _experiment_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "experiment_id": row["experiment_id"],
            "project_id": row["project_id"],
            "name": row["name"],
            "description": row["description"],
            "dataset_id": row["dataset_id"],
            "dataset_version": row["dataset_version"],
            "cohorts": json.loads(row["cohorts_json"] or "{}"),
            "evaluators": json.loads(row["evaluators_json"] or "[]"),
            "status": row["status"],
            "result_ref": row["result_ref"],
            "provenance": row["provenance"],
            "created_at": (
                d.isoformat() if (d := _dt(row["created_ns"])) else None
            ),
            "metadata": json.loads(row["metadata_json"] or "{}"),
        }

    def upsert_experiment(
        self,
        experiment_id: str,
        *,
        project_id: str = "default",
        name: str | None = None,
        description: str | None = None,
        dataset_id: str | None = None,
        dataset_version: str | None = None,
        cohorts: dict[str, Any] | None = None,
        evaluators: list[Any] | None = None,
        metadata: dict[str, Any] | None = None,
        provenance: str = "imported",
        now_ns: int | None = None,
    ) -> None:
        """Insert or update the experiment *spec*.

        Same convention as ``upsert_dataset``: the declared spec is
        mutable metadata — ``created_ns``, ``status`` and ``result_ref``
        are never touched here. Prior runs stay pinned in
        ``experiment_results``; each report embeds the resolved inputs,
        so re-spec'ing cannot rewrite history."""
        _require_utf8(
            {
                "experiment_id": experiment_id,
                "project_id": project_id,
                "name": name,
                "description": description,
                "dataset_id": dataset_id,
                "dataset_version": dataset_version,
                "provenance": provenance,
            }
        )
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO experiments(experiment_id, project_id, name,
                        description, dataset_id, dataset_version,
                        cohorts_json, evaluators_json, metadata_json,
                        provenance, created_ns)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(project_id, experiment_id) DO UPDATE SET
                       name=excluded.name, description=excluded.description,
                       dataset_id=excluded.dataset_id,
                       dataset_version=excluded.dataset_version,
                       cohorts_json=excluded.cohorts_json,
                       evaluators_json=excluded.evaluators_json,
                       metadata_json=excluded.metadata_json,
                       provenance=excluded.provenance""",
                (
                    experiment_id,
                    project_id,
                    name,
                    description,
                    dataset_id,
                    dataset_version,
                    json.dumps(_json_finite(cohorts or {}),
                               sort_keys=True, default=str),
                    json.dumps(_json_finite(evaluators or []),
                               sort_keys=True, default=str),
                    json.dumps(_json_finite(metadata or {}),
                               sort_keys=True, default=str),
                    provenance,
                    now_ns,
                ),
            )

    def get_experiment(
        self, experiment_id: str, project_id: str = "default"
    ) -> dict[str, Any] | None:
        if not _utf8_ok(experiment_id) or not _utf8_ok(project_id):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM experiments WHERE project_id=? AND experiment_id=?",
                (project_id, experiment_id),
            ).fetchone()
        if row is None:
            return None
        return self._experiment_dict(row)

    def list_experiments(
        self,
        project_id: str | None = None,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        _check_i64("limit", limit)
        _check_i64("offset", offset)
        if not _utf8_ok(project_id):
            return []
        with self._lock:
            if project_id is not None:
                rows = self._conn.execute(
                    "SELECT * FROM experiments WHERE project_id=? "
                    "ORDER BY experiment_id LIMIT ? OFFSET ?",
                    (project_id, max(0, limit), max(0, offset)),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM experiments "
                    "ORDER BY project_id, experiment_id LIMIT ? OFFSET ?",
                    (max(0, limit), max(0, offset)),
                ).fetchall()
        return [
            {
                "experiment_id": r["experiment_id"],
                "project_id": r["project_id"],
                "name": r["name"],
                "status": r["status"],
                "result_ref": r["result_ref"],
                "created_at": (
                    d.isoformat() if (d := _dt(r["created_ns"])) else None
                ),
            }
            for r in rows
        ]

    def set_experiment_status(
        self, experiment_id: str, status: str, *, project_id: str = "default"
    ) -> bool:
        """Explicit lifecycle transition (e.g. reopen). ``status`` is a
        caller-validated ``open|completed`` — the store trusts the
        boundary like it does for span status codes."""
        if not _utf8_ok(experiment_id) or not _utf8_ok(project_id):
            return False  # unencodable id can never match a stored row
        _require_utf8({"status": status})
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE experiments SET status=? "
                "WHERE project_id=? AND experiment_id=?",
                (status, project_id, experiment_id),
            )
        return cur.rowcount > 0

    def record_experiment_result(
        self,
        experiment_id: str,
        *,
        project_id: str = "default",
        report: dict[str, Any],
        run_ns: int | None = None,
    ) -> int:
        """Append one run report and advance the lifecycle atomically.

        Append-only: every execution is a new ``experiment_results`` row
        (``run:<id>`` becomes ``result_ref``), and the experiment flips
        to ``completed`` in the same transaction — a recorded run can
        never outlive the status that proves it happened."""
        _require_utf8({"experiment_id": experiment_id, "project_id": project_id})
        with self._lock, self._conn:
            exists = self._conn.execute(
                "SELECT 1 FROM experiments WHERE project_id=? AND experiment_id=?",
                (project_id, experiment_id),
            ).fetchone()
            if exists is None:
                raise ValidationError(
                    f"experiment {experiment_id!r} not found "
                    f"in project {project_id!r}"
                )
            cur = self._conn.execute(
                """INSERT INTO experiment_results(experiment_id, project_id,
                                                  run_ns, report_json)
                   VALUES (?,?,?,?)""",
                (
                    experiment_id,
                    project_id,
                    run_ns,
                    json.dumps(_json_finite(report),
                               sort_keys=True, default=str),
                ),
            )
            run_id = int(cur.lastrowid or 0)
            self._conn.execute(
                "UPDATE experiments SET status='completed', result_ref=? "
                "WHERE project_id=? AND experiment_id=?",
                (f"run:{run_id}", project_id, experiment_id),
            )
        return run_id

    def list_experiment_results(
        self,
        experiment_id: str,
        project_id: str = "default",
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        _check_i64("limit", limit)
        _check_i64("offset", offset)
        if not _utf8_ok(experiment_id) or not _utf8_ok(project_id):
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiment_results WHERE project_id=? "
                "AND experiment_id=? ORDER BY run_id LIMIT ? OFFSET ?",
                (project_id, experiment_id, max(0, limit), max(0, offset)),
            ).fetchall()
        return [
            {
                "run_id": r["run_id"],
                "experiment_id": r["experiment_id"],
                "project_id": r["project_id"],
                "run_at": (
                    d.isoformat() if (d := _dt(r["run_ns"])) else None
                ),
                "report": json.loads(r["report_json"] or "{}"),
            }
            for r in rows
        ]

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
    ) -> None:
        _require_utf8(
            {
                "key_id": key_id,
                "name": name,
                "key_hash": key_hash,
                "role": role,
            }
        )
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO api_keys(key_id, name, key_hash, role,
                                        projects_json, created_ns)
                   VALUES (?,?,?,?,?,?)""",
                (
                    key_id,
                    name,
                    key_hash,
                    role,
                    json.dumps(sorted(projects or [])),
                    now_ns,
                ),
            )

    def api_key_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Resolve a presented secret's hash → key record (or None).

        Never returns secret material — the table stores hashes only."""
        if not _utf8_ok(key_hash):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_hash=? AND revoked_ns IS NULL",
                (key_hash,),
            ).fetchone()
        if row is None:
            return None
        return self._api_key_dict(row)

    def get_api_key(self, key_id: str) -> dict[str, Any] | None:
        if not _utf8_ok(key_id):
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key_id=?", (key_id,)
            ).fetchone()
        return self._api_key_dict(row) if row else None

    def list_api_keys(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM api_keys ORDER BY key_id"
            ).fetchall()
        return [self._api_key_dict(r) for r in rows]

    @staticmethod
    def _api_key_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "key_id": row["key_id"],
            "name": row["name"],
            "role": row["role"],
            "projects": json.loads(row["projects_json"] or "[]"),
            "created_at": (
                d.isoformat() if (d := _dt(row["created_ns"])) else None
            ),
            "revoked_at": (
                d.isoformat() if (d := _dt(row["revoked_ns"])) else None
            ),
        }

    def revoke_api_key(self, key_id: str, *, now_ns: int | None = None) -> bool:
        if not _utf8_ok(key_id):
            return False  # unencodable id can never match a stored row
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE api_keys SET revoked_ns=? WHERE key_id=? AND revoked_ns IS NULL",
                (now_ns or 0, key_id),
            )
        return cur.rowcount > 0

    def record_audit(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
        ts_ns: int | None = None,
    ) -> None:
        """Append an audit event. Detail must never carry secrets —
        callers pass key ids/names, never key material."""
        _require_utf8(
            {
                "actor": actor,
                "action": action,
                "resource": resource,
                "outcome": outcome,
            }
        )
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO audit_events(ts_ns, actor, action, resource,"
                " outcome, detail_json) VALUES (?,?,?,?,?,?)",
                (
                    ts_ns or 0,
                    actor,
                    action,
                    resource,
                    outcome,
                    json.dumps(_json_finite(detail or {}),
                           sort_keys=True, default=str),
                ),
            )

    def list_audit(
        self,
        *,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        _check_i64("limit", limit)
        _check_i64("offset", offset)
        if not _utf8_ok(action) or not _utf8_ok(actor):
            return []
        clauses, params = [], []
        if action:
            clauses.append("action = ?")
            params.append(action)
        if actor:
            clauses.append("actor = ?")
            params.append(actor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM audit_events {where}
                    ORDER BY ts_ns DESC, event_id DESC LIMIT ? OFFSET ?""",
                (*params, max(0, limit), max(0, offset)),
            ).fetchall()
        return [
            {
                "event_id": r["event_id"],
                "ts_ns": r["ts_ns"],
                "ts": (d.isoformat() if (d := _dt(r["ts_ns"])) else None),
                "actor": r["actor"],
                "action": r["action"],
                "resource": r["resource"],
                "outcome": r["outcome"],
                "detail": json.loads(r["detail_json"] or "{}"),
            }
            for r in rows
        ]

    def prune_traces(
        self, *, before_ns: int, project_id: str | None = None
    ) -> dict[str, int]:
        """Retention: delete traces (and their spans) started before
        ``before_ns``. Bounded to trace granularity — partial-trace
        deletion would fabricate incomplete executions."""
        if not _utf8_ok(project_id):
            return {"traces": 0}  # unencodable id can never match
        clauses = ["started_ns < ?"]
        params: list[Any] = [before_ns]
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        where = " AND ".join(clauses)
        with self._lock, self._conn:
            doomed = self._conn.execute(
                f"SELECT project_id, trace_id FROM traces WHERE {where}",
                params,
            ).fetchall()
            for row in doomed:
                self._conn.execute(
                    "DELETE FROM spans WHERE project_id=? AND trace_id=?",
                    (row["project_id"], row["trace_id"]),
                )
                self._conn.execute(
                    "DELETE FROM traces WHERE project_id=? AND trace_id=?",
                    (row["project_id"], row["trace_id"]),
                )
        return {"traces": len(doomed)}

    def delete_trace_ids(
        self, trace_ids: list[str], *, project_id: str | None = None
    ) -> dict[str, int]:
        """Delete exactly the given trace_ids — archive's contract is
        'only what was exported', never a re-computed criterion."""
        doomed: list[tuple[str, str]] = []
        with self._lock, self._conn:
            for tid in trace_ids:
                if not _utf8_ok(tid) or not _utf8_ok(project_id):
                    continue  # an unencodable id was never stored
                rows = self._conn.execute(
                    "SELECT project_id, trace_id FROM traces WHERE trace_id = ?"
                    + (" AND project_id = ?" if project_id is not None else ""),
                    (tid, project_id) if project_id is not None else (tid,),
                ).fetchall()
                for row in rows:
                    self._conn.execute(
                        "DELETE FROM spans WHERE project_id=? AND trace_id=?",
                        (row["project_id"], row["trace_id"]),
                    )
                    self._conn.execute(
                        "DELETE FROM traces WHERE project_id=? AND trace_id=?",
                        (row["project_id"], row["trace_id"]),
                    )
                    doomed.append((row["project_id"], row["trace_id"]))
        return {"traces": len(doomed)}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
