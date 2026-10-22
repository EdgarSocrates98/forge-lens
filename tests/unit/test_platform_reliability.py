"""Platform reliability — the seams where the environment pushes back.

Coverage areas, one class each:
- filesystem paths: nesting, ``.``/``..``, spaces, unicode, symlinks,
  ``~``, relative-vs-absolute ``--db``, cwd independence
- encoding/unicode: accents/emoji/CJK/RTL round-trips, NFC vs NFD
  identity, lone surrogates (clean typed errors or honest "absent")
- file permissions: init layout/mode, unwritable dirs, readonly probes
- SQLite WAL: journal mode, snapshot isolation, busy_timeout, two
  writers, schema idempotence, readonly ghost files
- concurrency: N writers + M readers on the same file
- timeouts: a held write lock fails bounded, never hangs
- close/shutdown: locks released, idempotent close, data survives
- JSON edges: huge ints, non-finite floats, nesting depth, int64 binds
- env config: FORGE_LENS_* precedence/coercion, malformed TOML

Deterministic only: tmp_path isolation, events over sleeps, no real
signals, no network. Where behavior differs across platforms the test
documents *actual* behavior rather than guessing at intent.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from forge_lens.cli.main import app as cli_app
from forge_lens.contracts.v1.trace import (
    Span,
    SpanEvent,
    SpanLink,
    Status,
    StatusCode,
    Trace,
    TraceSource,
)
from forge_lens.core.config import DEFAULT_DIR_NAME, init_project, load_config
from forge_lens.core.errors import ValidationError
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.storage.sqlite import SCHEMA_VERSION, SQLiteTraceStore

runner = CliRunner()

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _mk_trace(
    tid: str = "a" * 32,
    *,
    project_id: str = "default",
    session_id: str | None = None,
    name: str = "op",
    status_message: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Trace:
    """Minimal one-span trace with valid hex ids."""
    span = Span(
        trace_id=tid,
        span_id="1" * 16,
        name=name,
        start_time=_T0,
        end_time=_T0 + timedelta(milliseconds=25),
        attributes=attributes or {},
        status=Status(code=StatusCode.OK, message=status_message),
    )
    return Trace(
        trace_id=tid,
        project_id=project_id,
        session_id=session_id,
        started_at=_T0,
        ended_at=span.end_time,
        status=Status(code=StatusCode.OK),
        source=TraceSource(system="pytest"),
        spans=[span],
    )


def _mk_trace_n(tid_int: int, n_spans: int = 2) -> Trace:
    """Deterministic distinct trace for concurrency loops."""
    tid = f"{tid_int:032x}"
    spans = [
        Span(
            trace_id=tid,
            span_id=f"{i + 1:016x}",
            name=f"op-{tid_int}-{i}",
            start_time=_T0,
            end_time=_T0 + timedelta(milliseconds=5),
            status=Status(code=StatusCode.OK),
        )
        for i in range(n_spans)
    ]
    return Trace(
        trace_id=tid,
        started_at=_T0,
        ended_at=spans[-1].end_time,
        status=Status(code=StatusCode.OK),
        source=TraceSource(system="pytest"),
        spans=spans,
    )


# -- task 1: filesystem paths --------------------------------------------


class TestFilesystemPaths:
    def test_nested_dirs_created_on_open(self, tmp_path: Path) -> None:
        db = tmp_path / "deep" / "nested" / "dir" / "lens.db"
        store = SQLiteTraceStore(db)
        assert db.exists()  # parent mkdir'd, db materialized by init_schema
        assert db.name == "lens.db"
        store.close()

    def test_dot_segments_and_dotdot_normalized(self, tmp_path: Path) -> None:
        weird = tmp_path / "sub" / ".." / "." / "lens.db"
        store = SQLiteTraceStore(weird)
        assert store.path == (tmp_path / "lens.db").resolve()
        assert (tmp_path / "lens.db").exists()
        assert not (tmp_path / "sub").exists()  # no stray dir from mkdir
        store.close()

    def test_paths_with_spaces_and_unicode(self, tmp_path: Path) -> None:
        db = tmp_path / "dados ☕ メトリクス" / "my lens db.sqlite"
        store = SQLiteTraceStore(db)
        store.upsert_trace(_mk_trace())
        assert store.get_trace("a" * 32) is not None
        store.close()
        ro = SQLiteTraceStore(db, readonly=True)
        assert ro.stats()["traces"] == 1
        ro.close()

    @pytest.mark.skipif(
        os.name == "nt", reason="symlink creation needs privilege on Windows"
    )
    def test_symlinked_data_dir(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        store = SQLiteTraceStore(link / "lens.db")
        store.upsert_trace(_mk_trace())
        # path resolves to the real dir — data lands where the link points
        assert store.path == (real / "lens.db").resolve()
        assert (real / "lens.db").exists()
        assert store.stats()["traces"] == 1
        store.close()

    def test_long_path(self, tmp_path: Path) -> None:
        db = tmp_path / ("d" * 120) / ("e" * 100) / "lens.db"
        store = SQLiteTraceStore(db)
        assert store.path.exists()
        store.close()

    def test_relative_db_survives_cwd_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        store = SQLiteTraceStore(Path("rel") / "lens.db")
        store.upsert_trace(_mk_trace())
        assert store.path.is_absolute()
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        # the open fd keeps reads/writes valid; self.path must stay bound
        # to the real file or stats() silently reports db_size 0
        assert store.stats()["db_size_bytes"] > 0
        assert store.get_trace("a" * 32) is not None
        store.close()

    def test_tilde_db_path_expands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))  # nt expansion
        monkeypatch.chdir(tmp_path)
        store = SQLiteTraceStore("~/lens-tilde.db")
        assert store.path == Path("~/lens-tilde.db").expanduser().resolve()
        assert (tmp_path / "lens-tilde.db").exists()
        # the bug this guards: sqlite never sees '~' — without expanduser
        # the path bound literally and created a './~' directory
        assert not (tmp_path / "~").exists()
        store.close()

    def test_db_path_that_is_a_directory(self, tmp_path: Path) -> None:
        with pytest.raises(sqlite3.OperationalError):
            SQLiteTraceStore(tmp_path / "subdir" / "..")
        with pytest.raises(sqlite3.OperationalError):
            SQLiteTraceStore(tmp_path, readonly=True)

    def test_parent_component_is_a_file(self, tmp_path: Path) -> None:
        blocker = tmp_path / "afile"
        blocker.write_text("x")
        with pytest.raises(OSError):
            SQLiteTraceStore(blocker / "lens.db")

    def test_cli_db_relative_then_absolute(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        clean_env: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.chdir(tmp_path)
        payload = tmp_path / "payload.json"
        payload.write_text(
            json.dumps(
                {
                    "project_id": "cli-rel",
                    "traces": [
                        _mk_trace("e" * 32).model_dump(mode="json")
                    ],
                }
            )
        )
        r = runner.invoke(cli_app, ["ingest", "payload.json", "--db", "rel.db"])
        assert r.exit_code == 0, r.output
        r = runner.invoke(
            cli_app,
            ["traces", "list", "--db", "rel.db", "--project", "cli-rel"],
        )
        assert r.exit_code == 0
        assert "e" * 32 in r.output
        # absolute --db must resolve from a different cwd too
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.chdir(other)
        r = runner.invoke(
            cli_app,
            [
                "traces",
                "list",
                "--db",
                str((tmp_path / "rel.db").resolve()),
                "--project",
                "cli-rel",
            ],
        )
        assert r.exit_code == 0
        assert "e" * 32 in r.output


# -- task 2: encoding / unicode ------------------------------------------

_ACCENTS = "reunião séance naïve Łódź"
_EMOJI = "😀🚀☕"
_CJK = "メトリクス"
_RTL = "ٱلْعَرَبِيَّة עִבְרִית"


class TestEncodingUnicode:
    def test_unicode_roundtrip_everywhere(
        self, tmp_path: Path
    ) -> None:
        session = f"{_ACCENTS} {_EMOJI}"
        name = f"{_CJK} {_RTL}"
        attrs = {
            "chave_ção": _ACCENTS,
            _EMOJI: [_CJK, _RTL],
            "rtl": _RTL,
        }
        trace = _mk_trace(
            "b" * 32,
            project_id="prój-☕",
            session_id=session,
            name=name,
            status_message="falhou — " + _EMOJI,
            attributes=attrs,
        )
        store = SQLiteTraceStore(tmp_path / "u.db")
        store.upsert_trace(trace)
        loaded = store.get_trace("b" * 32, project_id="prój-☕")
        assert loaded is not None
        assert loaded.session_id == session
        assert loaded.spans[0].name == name
        assert loaded.spans[0].attributes == attrs
        assert loaded.spans[0].status.message == "falhou — " + _EMOJI
        # unicode filters hit through the LIKE-escaped path too
        items = store.list_traces(project_id="prój-☕", q=_RTL[:6])
        assert [i["trace_id"] for i in items] == ["b" * 32]
        sessions = store.list_sessions("prój-☕")
        assert sessions[0]["session_id"] == session
        store.close()

    def test_nfc_nfd_are_distinct_identities(
        self, tmp_path: Path
    ) -> None:
        """Documented: no normalization — sqlite TEXT compares byte-wise,
        so 'café' (NFC) and 'café' (NFD) are different ids. Producers
        needing dedupe must normalize upstream."""
        nfc = "café"
        nfd = unicodedata.normalize("NFD", nfc)
        assert nfc != nfd
        store = SQLiteTraceStore(tmp_path / "nf.db")
        store.upsert_trace(
            _mk_trace("a" * 32, project_id=nfc, session_id="s", name=nfc)
        )
        store.upsert_trace(
            _mk_trace("b" * 32, project_id=nfd, session_id="s", name=nfd)
        )
        # same trace_id query under the other normalization is absent
        assert store.get_trace("a" * 32, project_id=nfc) is not None
        assert store.get_trace("a" * 32, project_id=nfd) is None
        assert len(store.list_traces(project_id=nfc)) == 1
        assert len(store.list_traces(project_id=nfd)) == 1
        # byte-wise LIKE: q=nfc must not match the nfd-named span
        hits = store.list_traces(q=nfc)
        assert {i["project_id"] for i in hits} == {nfc}
        store.close()

    def test_lone_surrogate_rejected_on_upsert(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteTraceStore(tmp_path / "s.db")
        for kw in (
            {"session_id": "sess\ud800"},
            {"name": "op\ud800"},
            {"status_message": "err\udfff"},
            {"project_id": "p\ud800"},
        ):
            with pytest.raises(ValidationError, match="lone surrogate"):
                store.upsert_trace(_mk_trace(**kw))
        assert store.stats()["traces"] == 0  # nothing half-written
        store.close()

    def test_lone_surrogate_reads_are_absent_not_tracebacks(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteTraceStore(tmp_path / "s2.db")
        store.upsert_trace(_mk_trace())
        bad = "x\ud800"
        assert store.get_trace(bad) is None
        assert store.get_trace("a" * 32, project_id=bad) is None
        assert store.list_traces(q=bad) == []
        assert store.list_traces(project_id=bad) == []
        assert store.list_traces(session_id=bad) == []
        assert store.list_traces(tool=bad, model=bad, agent=bad, host=bad) == []
        assert store.list_sessions(bad) == []
        assert store.list_tools(bad) == []
        assert store.stats(bad)["traces"] == 0
        assert store.span_trace_id("default", bad) is None
        store.close()

    def test_lone_surrogate_writes_raise_typed_error(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteTraceStore(tmp_path / "s3.db")
        bad = "x\ud800"
        with pytest.raises(ValidationError):
            store.upsert_dataset(bad, name="n")
        with pytest.raises(ValidationError):
            store.upsert_dataset_version(
                "d", "v1", cases=[{"case_id": bad}]
            )
        with pytest.raises(ValidationError):
            store.upsert_experiment(bad)
        with pytest.raises(ValidationError):
            store.record_experiment_result(bad, report={})
        with pytest.raises(ValidationError):
            store.create_api_key(bad, name="n", key_hash="h", role="reader")
        with pytest.raises(ValidationError):
            store.record_audit(
                actor=bad, action="a", resource="r", outcome="denied"
            )
        # reads/mutations on absent ids stay honest, not erroring
        assert store.get_dataset(bad) is None
        assert store.get_experiment(bad) is None
        assert store.get_api_key(bad) is None
        assert store.api_key_by_hash(bad) is None
        assert store.revoke_api_key(bad) is False
        assert store.set_experiment_status(bad, "open") is False
        assert store.list_audit(actor=bad) == []
        assert store.delete_trace_ids([bad]) == {"traces": 0}
        store.close()

    def test_surrogate_inside_attributes_roundtrips_as_json(
        self, tmp_path: Path
    ) -> None:
        """Attributes ride json.dumps escapes — '\\ud800' is valid JSON
        and stays storable. Only raw-bound TEXT columns reject lone
        surrogates (the API/pipeline reject them earlier anyway)."""
        trace = _mk_trace(attributes={"odd": "x\ud800"})
        store = SQLiteTraceStore(tmp_path / "s4.db")
        store.upsert_trace(trace)
        loaded = store.get_trace("a" * 32)
        assert loaded is not None
        assert loaded.spans[0].attributes["odd"] == "x\ud800"
        store.close()

    def test_pipeline_rejects_surrogate_before_any_write(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteTraceStore(tmp_path / "s5.db")
        pipe = IngestPipeline(store)
        with pytest.raises(ValidationError, match="lone surrogate"):
            pipe.ingest([_mk_trace(session_id="bad\ud800")])
        assert store.stats()["traces"] == 0
        store.close()


# -- task 3: file permissions ---------------------------------------------

_CHMOD_NEEDED = pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="POSIX chmod semantics; root bypasses permission bits",
)


class TestFilePermissions:
    def test_init_layout_and_dir_mode(self, tmp_path: Path) -> None:
        cfg = init_project(tmp_path, "proj-x")
        data_dir = tmp_path / DEFAULT_DIR_NAME
        assert data_dir.is_dir()
        assert (data_dir / "artifacts").is_dir()
        assert cfg.exists()
        # no explicit chmod is applied — mode is 0o777 masked by the
        # process umask; assert exactly that contract
        prev = os.umask(0o022)
        os.umask(prev)
        expected = 0o777 & ~prev
        assert stat.S_IMODE(data_dir.stat().st_mode) == expected
        # and the starter config round-trips
        loaded = load_config(tmp_path)
        assert loaded.project_id == "proj-x"
        assert loaded.data_dir == data_dir

    @_CHMOD_NEEDED
    def test_unwritable_parent_fails_with_typed_error(
        self, tmp_path: Path
    ) -> None:
        locked = tmp_path / "locked"
        locked.mkdir()
        os.chmod(locked, 0o555)  # noqa: S103 — the test needs a read-only dir
        try:
            with pytest.raises(sqlite3.OperationalError):
                SQLiteTraceStore(locked / "lens.db")
        finally:
            os.chmod(locked, 0o755)  # noqa: S103 — restore so tmp cleanup works

    @_CHMOD_NEEDED
    def test_db_in_readonly_dir_rw_open_fails_but_probe_reads(
        self, tmp_path: Path
    ) -> None:
        locked = tmp_path / "rod"
        locked.mkdir()
        store = SQLiteTraceStore(locked / "d.db")
        store.upsert_trace(_mk_trace())
        store.close()
        os.chmod(locked, 0o555)  # noqa: S103 — simulating a read-only mount
        try:
            # rw open must create -shm/-wal in a dir it can't write →
            # typed driver error, not a hang or a partial store
            with pytest.raises(sqlite3.OperationalError):
                SQLiteTraceStore(locked / "d.db")
            # the health probe still reads — immutable mode needs no
            # sidecars (this is the doctor-on-ro-mount case)
            probe = SQLiteTraceStore(locked / "d.db", readonly=True)
            assert probe.stats()["schema_version"] == SCHEMA_VERSION
            assert probe.stats()["traces"] == 1
            probe.close()
        finally:
            os.chmod(locked, 0o755)  # noqa: S103 — restore so tmp cleanup works


# -- task 4: SQLite WAL ----------------------------------------------------


class TestSqliteWAL:
    def test_wal_pragmas(self, tmp_path: Path) -> None:
        store = SQLiteTraceStore(tmp_path / "w.db")
        jm = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert jm == "wal"
        assert (
            store._conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        )  # NORMAL
        assert (
            store._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        )
        assert (
            store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        )
        store.close()

    def test_reader_sees_precommit_snapshot(self, tmp_path: Path) -> None:
        """WAL: a reader must see the last committed state while another
        connection holds an uncommitted write — no lock error, no torn
        read."""
        db = tmp_path / "snap.db"
        store = SQLiteTraceStore(db)
        holder = sqlite3.connect(db)
        holder.execute("PRAGMA busy_timeout=2000")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute(
            "INSERT INTO traces(trace_id, project_id, started_ns,"
            " source_json, first_seen_ns, last_seen_ns)"
            " VALUES ('f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0',"
            " 'default', 1, '{}', 1, 1)"
        )
        # uncommitted insert is invisible to the reader (pre-commit snap)
        assert store.list_traces() == []
        holder.commit()
        assert len(store.list_traces()) == 1
        holder.close()
        store.close()

    def test_init_schema_idempotent(self, tmp_path: Path) -> None:
        store = SQLiteTraceStore(tmp_path / "i.db")
        store.upsert_trace(_mk_trace())
        store.init_schema()
        store.init_schema()  # migrations/re-runs must not clobber data
        assert store.stats()["schema_version"] == SCHEMA_VERSION
        assert store.get_trace("a" * 32) is not None
        assert store.stats()["traces"] == 1
        store.close()

    def test_schema_version_survives_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "v.db"
        SQLiteTraceStore(db).close()
        store = SQLiteTraceStore(db)  # second open re-runs init_schema
        assert store.stats()["schema_version"] == SCHEMA_VERSION
        store.close()

    def test_readonly_open_missing_db_clean_error(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(sqlite3.OperationalError):
            SQLiteTraceStore(tmp_path / "nope" / "lens.db", readonly=True)
        # probe must not materialize anything — no dirs, no files
        assert not (tmp_path / "nope").exists()

    def test_readonly_probe_leaves_no_ghosts(self, tmp_path: Path) -> None:
        """A cold WAL db opened ro used to create -shm/-wal sidecars —
        the probe mutated the very directory it only inspected."""
        db = tmp_path / "lens.db"
        store = SQLiteTraceStore(db)
        store.upsert_trace(_mk_trace())
        store.close()
        before = {p.name for p in tmp_path.iterdir()}
        assert "lens.db-wal" not in before  # cold: checkpointed at close
        probe = SQLiteTraceStore(db, readonly=True)
        assert probe.stats()["traces"] == 1
        probe.close()
        assert {p.name for p in tmp_path.iterdir()} == before

    def test_readonly_rejects_writes(self, tmp_path: Path) -> None:
        db = tmp_path / "ro.db"
        SQLiteTraceStore(db).close()
        probe = SQLiteTraceStore(db, readonly=True)
        with pytest.raises(sqlite3.OperationalError):
            probe.upsert_trace(_mk_trace())
        probe.close()

    def test_readonly_sees_live_wal_data(self, tmp_path: Path) -> None:
        """An open writer keeps a live -wal — the probe must pick plain
        ro (immutable=1 would read only the main file and see zero
        rows), not just 'no -wal at check time'."""
        db = tmp_path / "live.db"
        writer = SQLiteTraceStore(db)
        writer.upsert_trace(_mk_trace())
        # writer stays open — committed rows live in the -wal file
        assert (tmp_path / "live.db-wal").exists()
        probe = SQLiteTraceStore(db, readonly=True)
        assert probe.stats()["traces"] == 1
        probe.close()
        writer.close()

    def test_readonly_tolerates_stray_shm(self, tmp_path: Path) -> None:
        """SQLite permits immutable=1 only when BOTH sidecars are gone —
        a lone -shm implies a WAL peer, so the probe takes plain ro and
        still reads the checkpointed file."""
        db = tmp_path / "shm.db"
        store = SQLiteTraceStore(db)
        store.upsert_trace(_mk_trace())
        store.close()
        (tmp_path / "shm.db-shm").write_bytes(b"\x00" * 32)
        probe = SQLiteTraceStore(db, readonly=True)
        assert probe.stats()["traces"] == 1
        probe.close()

    def test_readonly_with_uri_special_chars_in_name(
        self, tmp_path: Path
    ) -> None:
        """'?' and '%' must stay literal — an unencoded path would be
        parsed as URI query and silently open a different file. On
        Windows '?' can't exist in a filename at all, so the literal-'%'
        half carries the coverage there."""
        db = tmp_path / ("q%20x.db" if os.name == "nt" else "q%20?.db")
        store = SQLiteTraceStore(db)
        store.upsert_trace(_mk_trace())
        store.close()
        probe = SQLiteTraceStore(db, readonly=True)
        assert probe.stats()["traces"] == 1  # same file, not q%20→' ' or '?'
        probe.close()


# -- task 5: concurrency ---------------------------------------------------


class TestConcurrency:
    def test_concurrent_writers_and_readers(self, tmp_path: Path) -> None:
        """N writer connections + M readers on one WAL file: no
        exceptions, and final counts are exact — busy_timeout absorbs
        writer contention, readers never block."""
        db = tmp_path / "conc.db"
        n_writers, n_readers, n_each = 2, 2, 8
        stores = [SQLiteTraceStore(db) for _ in range(n_writers + n_readers)]
        writers, readers = stores[:n_writers], stores[n_writers:]
        done = threading.Event()
        errors: list[BaseException] = []

        def write_loop(store: SQLiteTraceStore, base: int) -> None:
            try:
                for i in range(n_each):
                    store.upsert_trace(_mk_trace_n(base + i))
            except BaseException as exc:
                errors.append(exc)

        def read_loop(store: SQLiteTraceStore) -> None:
            try:
                while not done.is_set():
                    store.list_traces(limit=500)
                    store.stats()
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=write_loop, args=(w, base * 0x100))
            for base, w in enumerate(writers)
        ] + [threading.Thread(target=read_loop, args=(r,)) for r in readers]
        try:
            for t in threads:
                t.start()
            for t in threads[:n_writers]:
                t.join(timeout=30)
            done.set()
            for t in threads[n_writers:]:
                t.join(timeout=30)
            assert errors == []
            assert all(not t.is_alive() for t in threads)
            # every committed write is durably visible
            check = SQLiteTraceStore(db)
            assert check.stats()["traces"] == n_writers * n_each
            assert check.stats()["spans"] == n_writers * n_each * 2
            check.close()
        finally:
            done.set()
            for s in stores:
                s.close()


# -- task 6: timeouts -------------------------------------------------------


class TestBusyTimeout:
    def test_held_write_lock_fails_bounded_not_hangs(
        self, tmp_path: Path
    ) -> None:
        """busy_timeout is a bound, not a deadlock: a contended write
        waits ~timeout then raises OperationalError, and the store is
        usable the moment the holder commits."""
        db = tmp_path / "busy.db"
        store = SQLiteTraceStore(db)
        holder = sqlite3.connect(db)
        holder.execute("PRAGMA busy_timeout=2000")
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO lens_meta(key, value) VALUES('h','1')")
        # shrink the wait the test must prove out (default is 5000ms)
        store._conn.execute("PRAGMA busy_timeout=250")
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            store.upsert_trace(_mk_trace())
        elapsed = time.monotonic() - t0
        assert elapsed < 4.0  # bounded — gave up instead of hanging
        assert elapsed >= 0.1  # honored the ~250ms budget before failing
        holder.commit()
        store.upsert_trace(_mk_trace())  # lock released → healthy again
        assert store.stats()["traces"] == 1
        holder.close()
        store.close()


# -- task 7: close / shutdown ------------------------------------------------


class TestCloseAndShutdown:
    def test_close_releases_file_locks(self, tmp_path: Path) -> None:
        db = tmp_path / "c.db"
        store = SQLiteTraceStore(db)
        store.upsert_trace(_mk_trace())
        store.close()
        # an EXCLUSIVE lock on a fresh conn succeeding immediately proves
        # close() released every lock the store held
        probe = sqlite3.connect(db, timeout=1)
        probe.execute("BEGIN EXCLUSIVE")
        probe.rollback()
        probe.close()
        # and a second store opens + writes right away
        store2 = SQLiteTraceStore(db)
        assert store2.upsert_trace(_mk_trace()) == (0, 0, 1)
        store2.close()

    def test_close_is_idempotent_and_use_after_close_is_typed(
        self, tmp_path: Path
    ) -> None:
        store = SQLiteTraceStore(tmp_path / "d.db")
        store.close()
        store.close()  # double close is a no-op, not an error
        with pytest.raises(sqlite3.ProgrammingError):
            store.list_traces()
        with pytest.raises(sqlite3.ProgrammingError):
            store.upsert_trace(_mk_trace())

    def test_data_survives_close_reopen(self, tmp_path: Path) -> None:
        db = tmp_path / "r.db"
        store = SQLiteTraceStore(db)
        store.upsert_trace(_mk_trace())
        store.close()
        # reopen sees committed data — WAL checkpoint on close makes the
        # main file self-contained again
        store2 = SQLiteTraceStore(db)
        assert store2.stats()["traces"] == 1
        assert store2.get_trace("a" * 32) is not None
        store2.close()


# -- task 8: JSON parsing edges (store/query level) --------------------------


class TestJsonEdges:
    def test_huge_ints_in_attributes_roundtrip(
        self, tmp_path: Path
    ) -> None:
        attrs = {"big": 2**100, "neg": -(2**100), "nested": {"v": 2**80}}
        store = SQLiteTraceStore(tmp_path / "j.db")
        store.upsert_trace(_mk_trace(attributes=attrs))
        loaded = store.get_trace("a" * 32)
        assert loaded is not None
        assert loaded.spans[0].attributes == attrs
        store.close()

    def test_int64_bounds_on_filters(self, tmp_path: Path) -> None:
        store = SQLiteTraceStore(tmp_path / "j2.db")
        store.upsert_trace(_mk_trace())
        over = 2**63
        under = -(2**63) - 1
        for kw in (
            {"limit": over},
            {"offset": over},
            {"since_ns": over},
            {"until_ns": under},
            {"min_duration_ns": over},
            {"max_duration_ns": under},
            {"min_total_tokens": over},
        ):
            with pytest.raises(ValueError, match=r"int64|non-negative"):
                store.list_traces(**kw)
        with pytest.raises(ValueError, match="non-negative"):
            store.list_traces(min_total_tokens=-1)
        # negative limit/offset clamp to 0 — honest pagination edge
        assert store.list_traces(limit=-5, offset=-3) == []
        store.close()

    def test_non_finite_floats_persist_as_null(
        self, tmp_path: Path
    ) -> None:
        """NaN/±inf have no JSON literal — _json_finite stores them as
        null so json_valid() stays 1 (attrs, events AND links)."""
        t0 = _T0
        span = Span(
            trace_id="a" * 32,
            span_id="1" * 16,
            name="op",
            start_time=t0,
            end_time=t0 + timedelta(milliseconds=1),
            attributes={"nan": float("nan"), "pinf": float("inf")},
            events=[
                SpanEvent(name="e", time=t0, attributes={"v": float("-inf")})
            ],
            links=[
                SpanLink(
                    trace_id="f" * 32,
                    span_id="f" * 16,
                    attributes={"w": float("nan")},
                )
            ],
            status=Status(code=StatusCode.OK),
        )
        trace = Trace(
            trace_id="a" * 32,
            started_at=t0,
            source=TraceSource(system="pytest"),
            resource_attributes={"r": float("inf")},
            spans=[span],
        )
        store = SQLiteTraceStore(tmp_path / "j3.db")
        store.upsert_trace(trace)
        # every stored document is still valid JSON
        rows = store._conn.execute(
            "SELECT json_valid(attributes_json) a, json_valid(events_json) e,"
            " json_valid(links_json) l FROM spans"
        ).fetchone()
        assert (rows["a"], rows["e"], rows["l"]) == (1, 1, 1)
        loaded = store.get_trace("a" * 32)
        assert loaded is not None
        assert loaded.spans[0].attributes == {"nan": None, "pinf": None}
        assert loaded.spans[0].events[0].attributes == {"v": None}
        assert loaded.spans[0].links[0].attributes == {"w": None}
        assert loaded.resource_attributes == {"r": None}
        store.close()

    def test_deep_but_bounded_attribute_nesting(
        self, tmp_path: Path
    ) -> None:
        """Well inside the app's 512-level guard, nested attrs must
        round-trip — the store itself has no depth limit."""
        leaf: dict[str, Any] = {}
        root: dict[str, Any] = leaf
        for _i in range(128):
            nxt: dict[str, Any] = {}
            leaf["k"] = nxt
            leaf = nxt
        leaf["end"] = True
        store = SQLiteTraceStore(tmp_path / "j4.db")
        store.upsert_trace(_mk_trace(attributes={"deep": root}))
        loaded = store.get_trace("a" * 32)
        assert loaded is not None
        cur = loaded.spans[0].attributes["deep"]
        for _ in range(128):
            cur = cur["k"]
        assert cur["end"] is True
        store.close()

    def test_like_metacharacters_match_literally(
        self, tmp_path: Path
    ) -> None:
        """q/tool filters are LIKE-escaped: '%', '_' and '\\' are literal
        characters, never wildcards injected from user input."""
        store = SQLiteTraceStore(tmp_path / "j5.db")
        store.upsert_trace(_mk_trace("a" * 32, name="10% complete"))
        store.upsert_trace(_mk_trace("b" * 32, name="10x complete"))
        hits = store.list_traces(q="10%")
        assert [i["trace_id"] for i in hits] == ["a" * 32]
        # '_' is literal too: '10_ comp' would wildcard-match both if unescaped
        hits = store.list_traces(q="10_")
        assert hits == []
        store.close()


# -- task 9: env-var config --------------------------------------------------

_LENS_ENV = [
    "FORGE_LENS_PROJECT",
    "FORGE_LENS_DB",
    "FORGE_LENS_CAPTURE",
    "FORGE_LENS_API_KEY",
    "FORGE_LENS_AUTH_READS",
    "FORGE_LENS_AUTH_MODE",
    "FORGE_LENS_RETENTION_DAYS",
    "FORGE_LENS_ENDPOINT",
    "FORGE_LENS_PRICING_FILE",
    "FORGE_LENS_MAX_PAYLOAD_BYTES",
    "FORGE_LENS_MAX_TRACES_PER_REQUEST",
    "FORGE_LENS_MAX_SPANS_PER_REQUEST",
    "FORGE_LENS_MAX_EVENTS_PER_SPAN",
    "FORGE_LENS_MAX_ATTR_VALUE_CHARS",
    "FORGE_LENS_RATE_LIMIT_PER_MINUTE",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Isolate from any FORGE_LENS_* present in the dev environment."""
    for key in _LENS_ENV:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


class TestEnvConfig:
    def test_env_db_wins_over_toml(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / DEFAULT_DIR_NAME
        data_dir.mkdir()
        (data_dir / "config.toml").write_text(
            '[lens]\ndb_path = "toml.db"\nproject_id = "toml-proj"\n'
        )
        env_db = tmp_path / "env.db"
        clean_env.setenv("FORGE_LENS_DB", str(env_db))
        clean_env.setenv("FORGE_LENS_PROJECT", "env-proj")
        cfg = load_config(tmp_path)
        assert cfg.resolved_db_path == env_db
        assert cfg.project_id == "env-proj"

    def test_env_overrides_every_mapped_field(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("FORGE_LENS_CAPTURE", "full")
        clean_env.setenv("FORGE_LENS_API_KEY", "k3y")
        clean_env.setenv("FORGE_LENS_AUTH_READS", "true")
        clean_env.setenv("FORGE_LENS_AUTH_MODE", "keys")
        clean_env.setenv("FORGE_LENS_RETENTION_DAYS", "30")
        clean_env.setenv("FORGE_LENS_ENDPOINT", "http://127.0.0.1:9999")
        clean_env.setenv("FORGE_LENS_MAX_PAYLOAD_BYTES", "1024")
        clean_env.setenv("FORGE_LENS_MAX_SPANS_PER_REQUEST", "500")
        clean_env.setenv("FORGE_LENS_MAX_EVENTS_PER_SPAN", "7")
        clean_env.setenv("FORGE_LENS_MAX_ATTR_VALUE_CHARS", "64")
        clean_env.setenv("FORGE_LENS_RATE_LIMIT_PER_MINUTE", "600")
        clean_env.setenv("FORGE_LENS_MAX_TRACES_PER_REQUEST", "42")
        clean_env.setenv("FORGE_LENS_PRICING_FILE", str(tmp_path / "p.json"))
        cfg = load_config(tmp_path)
        assert cfg.capture.value == "full"
        assert cfg.api_key == "k3y"
        assert cfg.auth_reads is True
        assert cfg.auth_mode == "keys"
        assert cfg.retention_days == 30
        assert cfg.endpoint == "http://127.0.0.1:9999"
        assert cfg.max_payload_bytes == 1024
        assert cfg.max_spans_per_request == 500
        assert cfg.max_events_per_span == 7
        assert cfg.max_attr_value_chars == 64
        assert cfg.rate_limit_per_minute == 600
        assert cfg.max_traces_per_request == 42
        assert cfg.pricing_file == tmp_path / "p.json"

    def test_bad_int_env_names_the_variable(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("FORGE_LENS_RETENTION_DAYS", "thirty")
        with pytest.raises(ValueError, match="FORGE_LENS_RETENTION_DAYS"):
            load_config(tmp_path)

    @pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "TRUE"])
    def test_auth_reads_truthy_values(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch, raw: str
    ) -> None:
        clean_env.setenv("FORGE_LENS_AUTH_READS", raw)
        assert load_config(tmp_path).auth_reads is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", "garbage"])
    def test_auth_reads_falsy_values(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch, raw: str
    ) -> None:
        clean_env.setenv("FORGE_LENS_AUTH_READS", raw)
        # documented: anything outside the truthy set is False — silent
        # coercion, not a config error
        assert load_config(tmp_path).auth_reads is False

    def test_auth_mode_env_validated(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("FORGE_LENS_AUTH_MODE", "bogus")
        with pytest.raises(ValueError, match="auth_mode"):
            load_config(tmp_path)

    def test_capture_env_validated(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("FORGE_LENS_CAPTURE", "everything")
        with pytest.raises(ValueError, match="FORGE_LENS_CAPTURE"):
            load_config(tmp_path)

    def test_env_db_expanduser(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        clean_env.setenv("HOME", str(tmp_path))
        clean_env.setenv("USERPROFILE", str(tmp_path))
        clean_env.setenv("FORGE_LENS_DB", "~/env-tilde.db")
        cfg = load_config(tmp_path)
        assert cfg.resolved_db_path == Path("~/env-tilde.db").expanduser()

    def test_malformed_toml_names_the_file(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / DEFAULT_DIR_NAME
        data_dir.mkdir()
        cfg_file = data_dir / "config.toml"
        cfg_file.write_text("this is = not = valid toml [[[")
        with pytest.raises(ValueError, match="invalid config file"):
            load_config(tmp_path)

    def test_toml_unknown_keys_tolerated(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / DEFAULT_DIR_NAME
        data_dir.mkdir()
        (data_dir / "config.toml").write_text(
            '[lens]\nproject_id = "p1"\nfuture_option = "x"\n'
            "unknown_int = 5\n"
        )
        cfg = load_config(tmp_path)
        assert cfg.project_id == "p1"  # unknown keys ignored, not errors

    def test_toml_flat_keys_and_wrong_types(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        data_dir = tmp_path / DEFAULT_DIR_NAME
        data_dir.mkdir()
        # no [lens] section → top level is read as the section
        (data_dir / "config.toml").write_text(
            'project_id = "flat"\n'
            "retention_days = true\n"  # bool is not an int — ignored
            "auth_mode = \"kyes\"\n"  # invalid: silently ignored (asymmetry
            # with env vars, which hard-fail — documented behavior)
            "max_payload_bytes = 1.5\n"  # float, not int — ignored
        )
        cfg = load_config(tmp_path)
        assert cfg.project_id == "flat"
        assert cfg.retention_days == 0
        assert cfg.auth_mode == "none"
        assert cfg.max_payload_bytes == 8 * 1024 * 1024

    def test_project_dir_resolution_and_cwd_default(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        # explicit project_dir → data_dir under it, no XDG lookup
        cfg = load_config(tmp_path)
        assert cfg.data_dir == tmp_path / DEFAULT_DIR_NAME
        assert cfg.resolved_db_path == (
            tmp_path / DEFAULT_DIR_NAME / "lens.db"
        )
        # no project_dir → cwd-relative (cwd at load_config call time)
        other = tmp_path / "other"
        other.mkdir()
        clean_env.chdir(other)
        cfg2 = load_config()
        assert cfg2.data_dir == other / DEFAULT_DIR_NAME


# -- API-level edges (TestClient — the same inputs over HTTP) -----------------


@pytest.fixture
def api_client(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from forge_lens.api.app import create_app
    from forge_lens.core.config import LensConfig

    store = SQLiteTraceStore(tmp_path / "api.db")
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db")
    with TestClient(create_app(store=store, config=cfg)) as client:
        yield client
    store.close()


def _trace_body(tid: str, **kw: Any) -> dict[str, Any]:
    """Minimal TraceIngest-compatible body."""
    trace = _mk_trace(tid, **kw).model_dump(mode="json")
    return {"traces": [trace]}


class TestApiEdges:
    def test_unicode_ingest_and_retrieve(self, api_client) -> None:
        body = _trace_body(
            "c" * 32,
            session_id="sessão 😀",
            name="メトリクス ٱلْعَرَبِيَّة",
            attributes={"chave": "☕", "rtl": "עִבְרִית"},
        )
        r = api_client.post("/v1/traces", json=body)
        assert r.status_code == 200, r.text
        r = api_client.get("/v1/traces/" + "c" * 32)
        assert r.status_code == 200
        trace = r.json()
        assert trace["session_id"] == "sessão 😀"
        assert trace["spans"][0]["name"] == "メトリクス ٱلْعَرَبِيَّة"
        assert trace["spans"][0]["attributes"]["chave"] == "☕"

    def test_surrogate_body_native_400(self, api_client) -> None:
        """json.loads accepts \\ud800 escapes but the native parser
        (jiter) refuses them → 400 invalid JSON, never a 500."""
        body = _trace_body("a" * 32, session_id="s\ud800")
        raw = json.dumps(body)
        assert "\\ud800" in raw
        r = api_client.post(
            "/v1/traces",
            content=raw.encode(),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400, r.text

    def test_surrogate_body_otlp_422(self, api_client) -> None:
        """The OTLP path parses with json.loads then screens strings —
        a lone surrogate is a typed 422."""
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {
                                    "traceId": "b" * 32,
                                    "spanId": "d" * 16,
                                    "name": "x\ud800",
                                    "startTimeUnixNano": "1",
                                }
                            ]
                        }
                    ]
                }
            ]
        }
        r = api_client.post(
            "/v1/otlp/traces",
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 422, r.text

    def test_wtf8_percent_query_is_not_a_500(self, api_client) -> None:
        """%ED%A0%80 is WTF-8 for a surrogate — starlette decodes it to
        U+FFFD, which simply matches nothing. Absent, never a crash."""
        r = api_client.get("/v1/traces?q=%ED%A0%80")
        assert r.status_code == 200
        assert r.json()["items"] == []
        r = api_client.get("/v1/traces/%ED%A0%80")
        assert r.status_code == 404

    def test_int64_and_enum_query_bounds(self, api_client) -> None:
        assert api_client.get(
            "/v1/traces", params={"offset": 2**63}
        ).status_code == 422
        assert api_client.get(
            "/v1/traces", params={"min_total_tokens": -1}
        ).status_code == 422
        assert api_client.get(
            "/v1/traces", params={"status": "bogus"}
        ).status_code == 422
        assert api_client.get(
            "/v1/traces", params={"since": "not-a-date"}
        ).status_code == 422
        # limit is clamped, not rejected
        r = api_client.get("/v1/traces", params={"limit": 10**30})
        assert r.status_code == 200
        assert r.json()["limit"] == 500

    def test_doctor_probe_leaves_no_ghosts(
        self, tmp_path: Path, clean_env: pytest.MonkeyPatch
    ) -> None:
        """End-to-end for the readonly fix: `lens doctor` on a cold db
        must not litter -shm/-wal next to it."""
        (tmp_path / DEFAULT_DIR_NAME).mkdir()  # doctor checks data dir exists
        db = tmp_path / "lens.db"
        store = SQLiteTraceStore(db)
        store.upsert_trace(_mk_trace())
        store.close()
        before = {p.name for p in tmp_path.iterdir()}
        r = runner.invoke(cli_app, ["doctor", str(tmp_path), "--db", str(db)])
        assert r.exit_code == 0, r.output
        assert "[ok] sqlite store readable" in r.output
        assert {p.name for p in tmp_path.iterdir()} == before
