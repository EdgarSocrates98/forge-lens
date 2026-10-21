"""Trace-list filters (store + API) and the /v1/sessions roll-up.

The seeded set is deliberately small and deterministic — six traces
across two projects, three sessions, mixed statuses, one un-ended
pair — so every filter's semantics is observable by inspection.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from forge_lens.api.app import create_app
from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.core.config import LensConfig
from forge_lens.core.time import to_unix_nano
from forge_lens.storage.sqlite import SQLiteTraceStore

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
T0_NS = to_unix_nano(T0)
_MIN = 60_000_000_000  # one minute, in ns


def _trace(
    trace_id: str,
    *,
    session_id: str | None = None,
    project_id: str = "default",
    status: StatusCode = StatusCode.OK,
    started: datetime = T0,
    duration_ms: float | None = 100,
    span_name: str = "op",
    attributes: dict[str, Any] | None = None,
) -> Trace:
    """One-span trace; ``duration_ms=None`` leaves it un-ended."""
    ended = (
        started + timedelta(milliseconds=duration_ms)
        if duration_ms is not None
        else None
    )
    span = Span(
        trace_id=trace_id,
        span_id="1" * 16,
        name=span_name,
        kind=SpanKind.TOOL,
        start_time=started,
        end_time=ended,
        attributes=attributes or {},
        status=Status(code=status),
    )
    return Trace(
        trace_id=trace_id,
        project_id=project_id,
        session_id=session_id,
        started_at=started,
        ended_at=ended,
        status=Status(code=status),
        spans=[span],
    )


def _fixture_traces() -> list[Trace]:
    return [
        # sess-1: an errored 900ms web_search call
        _trace(
            "a" * 32,
            session_id="sess-1",
            status=StatusCode.ERROR,
            started=T0,
            duration_ms=900,
            span_name="tool web_search",
            attributes={"tool.name": "web_search"},
        ),
        # sess-1: a fast ok call to a tool whose name contains a literal '%'
        _trace(
            "b" * 32,
            session_id="sess-1",
            started=T0 + timedelta(minutes=5),
            duration_ms=50,
            span_name="calc",
            attributes={"tool.name": "calc%"},
        ),
        # sess-2: still running (no end evidence)
        _trace(
            "c" * 32,
            session_id="sess-2",
            status=StatusCode.UNSET,
            started=T0 + timedelta(minutes=2),
            duration_ms=None,
            span_name="long op",
        ),
        # p2 tenant, no session — must never leak into default reads
        _trace(
            "d" * 32,
            project_id="p2",
            started=T0 + timedelta(minutes=1),
            duration_ms=500,
            span_name="p2-op",
        ),
        # sess-2: a second, completed trace (tool name is a '%'-decoy)
        _trace(
            "e" * 32,
            session_id="sess-2",
            started=T0 + timedelta(minutes=8),
            duration_ms=300,
            span_name="calculator",
            attributes={"tool.name": "calculator"},
        ),
        # sess-3: newest activity is an *un-ended* start — exercises the
        # NULL-end COALESCE in the session roll-up
        _trace(
            "f" * 32,
            session_id="sess-3",
            status=StatusCode.UNSET,
            started=T0 + timedelta(minutes=20),
            duration_ms=None,
            span_name="watcher",
        ),
    ]


def _seed(store: SQLiteTraceStore) -> None:
    for t in _fixture_traces():
        store.upsert_trace(t)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[SQLiteTraceStore]:
    s = SQLiteTraceStore(tmp_path / "filters.db")
    _seed(s)
    yield s
    s.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    s = SQLiteTraceStore(tmp_path / "api.db")
    _seed(s)
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db")
    app = create_app(store=s, config=cfg)
    with TestClient(app) as c:
        yield c
    s.close()


def _ids(items: list[dict[str, Any]]) -> set[str]:
    return {str(i["trace_id"]) for i in items}


# -- store: individual filters ----------------------------------------------


def test_filter_status(store: SQLiteTraceStore) -> None:
    assert _ids(store.list_traces(status="error")) == {"a" * 32}
    assert _ids(store.list_traces(status="ok")) == {"b" * 32, "d" * 32, "e" * 32}
    assert _ids(store.list_traces(status="unset")) == {"c" * 32, "f" * 32}


def test_filter_session_id_exact(store: SQLiteTraceStore) -> None:
    assert _ids(store.list_traces(session_id="sess-1")) == {"a" * 32, "b" * 32}
    assert store.list_traces(session_id="sess-9") == []


def test_filter_since_until_started_range(store: SQLiteTraceStore) -> None:
    # inclusive bounds on started_ns: a=T0, d=T0+1, c=T0+2, b=T0+5, e=T0+8
    assert _ids(store.list_traces(since_ns=T0_NS + 3 * _MIN)) == {
        "b" * 32,
        "e" * 32,
        "f" * 32,
    }
    assert _ids(store.list_traces(until_ns=T0_NS + _MIN)) == {
        "a" * 32,
        "d" * 32,
    }
    assert _ids(
        store.list_traces(since_ns=T0_NS + _MIN, until_ns=T0_NS + 5 * _MIN)
    ) == {"b" * 32, "c" * 32, "d" * 32}
    # a boundary instant is inclusive on both sides
    assert _ids(
        store.list_traces(since_ns=T0_NS, until_ns=T0_NS)
    ) == {"a" * 32}


def test_filter_duration(store: SQLiteTraceStore) -> None:
    # durations: a=900ms, b=50ms, d=500ms, e=300ms; c/f have no end
    assert _ids(store.list_traces(min_duration_ns=800_000_000)) == {"a" * 32}
    assert _ids(store.list_traces(max_duration_ns=400_000_000)) == {
        "b" * 32,
        "e" * 32,
    }
    assert _ids(
        store.list_traces(
            min_duration_ns=400_000_000, max_duration_ns=800_000_000
        )
    ) == {"d" * 32}


def test_duration_filter_excludes_unended_traces(
    store: SQLiteTraceStore,
) -> None:
    """Documented rule: NULL ended_ns has no observable duration, so ANY
    duration bound — even min=0 — excludes still-running traces rather
    than guessing a duration for them."""
    assert _ids(store.list_traces(min_duration_ns=0)) == {
        "a" * 32,
        "b" * 32,
        "d" * 32,
        "e" * 32,
    }
    assert _ids(store.list_traces(max_duration_ns=10**15)) == {
        "a" * 32,
        "b" * 32,
        "d" * 32,
        "e" * 32,
    }
    # without a duration filter the un-ended pair is visible
    assert {"c" * 32, "f" * 32} <= _ids(store.list_traces())


def test_filter_q_substring_trace_id_and_span_name(
    store: SQLiteTraceStore,
) -> None:
    assert _ids(store.list_traces(q="aaaa")) == {"a" * 32}  # trace_id hit
    assert _ids(store.list_traces(q="web_search")) == {"a" * 32}  # span name
    assert _ids(store.list_traces(q="calc")) == {"b" * 32, "e" * 32}
    assert store.list_traces(q="zzz-no-such") == []


def test_filter_tool_substring(store: SQLiteTraceStore) -> None:
    assert _ids(store.list_traces(tool="web")) == {"a" * 32}
    assert _ids(store.list_traces(tool="calc")) == {"b" * 32, "e" * 32}
    assert store.list_traces(tool="no-such-tool") == []


def test_like_metacharacters_are_literal_not_wildcards(
    store: SQLiteTraceStore,
) -> None:
    """A '%' or '_' in the search term must match the literal char —
    unescaped, both would widen into match-everything wildcards."""
    # tool.name 'calc%' matches; 'calculator' must NOT be swept in
    assert _ids(store.list_traces(tool="calc%")) == {"b" * 32}
    assert _ids(store.list_traces(tool="%")) == {"b" * 32}
    # '_' is literal: only 'web_search' carries one in tool.name/span name
    assert _ids(store.list_traces(tool="_")) == {"a" * 32}
    assert _ids(store.list_traces(q="_")) == {"a" * 32}


def test_filters_combine_with_and(store: SQLiteTraceStore) -> None:
    assert _ids(
        store.list_traces(session_id="sess-1", status="ok")
    ) == {"b" * 32}
    assert _ids(
        store.list_traces(
            status="ok", project_id="default", max_duration_ns=400_000_000
        )
    ) == {"b" * 32, "e" * 32}
    assert _ids(
        store.list_traces(q="calc", tool="calculator")
    ) == {"e" * 32}
    # contradictory predicates → empty, never a union
    assert (
        store.list_traces(session_id="sess-1", status="unset") == []
    )
    assert (
        store.list_traces(session_id="sess-1", status="error", q="calc")
        == []
    )


def test_project_id_semantics_unchanged(store: SQLiteTraceStore) -> None:
    """project_id=None stays the all-projects wildcard; a named project
    scopes the same filter set."""
    assert len(store.list_traces()) == 6
    assert _ids(store.list_traces(project_id="p2")) == {"d" * 32}
    assert _ids(store.list_traces(project_id="default", status="ok")) == {
        "b" * 32,
        "e" * 32,
    }
    assert _ids(
        store.list_traces(project_id="p2", min_duration_ns=0)
    ) == {"d" * 32}


def test_limit_offset_semantics_unchanged(store: SQLiteTraceStore) -> None:
    page1 = store.list_traces(limit=2)
    page2 = store.list_traces(limit=2, offset=2)
    # started DESC: f(+20m), e(+8m), b(+5m), c(+2m), d(+1m), a(0)
    assert [i["trace_id"] for i in page1] == ["f" * 32, "e" * 32]
    assert [i["trace_id"] for i in page2] == ["b" * 32, "c" * 32]


# -- store: list_sessions ----------------------------------------------------


def test_list_sessions_shape_and_rollup(store: SQLiteTraceStore) -> None:
    sessions = store.list_sessions()
    assert [s["session_id"] for s in sessions] == [
        "sess-3",
        "sess-2",
        "sess-1",
    ]
    for s in sessions:
        assert set(s) == {
            "session_id",
            "traces",
            "spans",
            "first_started_at",
            "last_activity_at",
        }
    by_id = {s["session_id"]: s for s in sessions}
    s1 = by_id["sess-1"]
    assert s1["traces"] == 2
    assert s1["spans"] == 2
    assert s1["first_started_at"] == T0.isoformat()
    # latest activity = b's end (T0+5min+50ms)
    assert s1["last_activity_at"] == (
        T0 + timedelta(minutes=5, milliseconds=50)
    ).isoformat()
    s2 = by_id["sess-2"]
    assert s2["traces"] == 2
    assert s2["first_started_at"] == (T0 + timedelta(minutes=2)).isoformat()
    assert s2["last_activity_at"] == (
        T0 + timedelta(minutes=8, milliseconds=300)
    ).isoformat()
    # sess-3's only trace never ended — its start IS the last activity,
    # and it still sorts first (newest activity wins)
    s3 = by_id["sess-3"]
    assert s3["traces"] == 1
    assert s3["last_activity_at"] == (T0 + timedelta(minutes=20)).isoformat()


def test_list_sessions_project_scope_and_paging(
    store: SQLiteTraceStore,
) -> None:
    # the p2 trace has no session — p2 rolls up to nothing
    assert store.list_sessions("p2") == []
    assert {s["session_id"] for s in store.list_sessions("default")} == {
        "sess-1",
        "sess-2",
        "sess-3",
    }
    page = store.list_sessions(limit=1)
    assert [s["session_id"] for s in page] == ["sess-3"]
    rest = store.list_sessions(limit=10, offset=1)
    assert [s["session_id"] for s in rest] == ["sess-2", "sess-1"]
    # session-less traces never form a session row
    assert all(s["session_id"] for s in store.list_sessions())


# -- API: GET /v1/traces params ----------------------------------------------


def test_api_traces_filter_params(client: TestClient) -> None:
    r = client.get("/v1/traces", params={"status": "error"})
    assert r.status_code == 200
    assert _ids(r.json()["items"]) == {"a" * 32}

    r = client.get("/v1/traces", params={"session_id": "sess-1"})
    assert _ids(r.json()["items"]) == {"a" * 32, "b" * 32}

    # ISO-8601 → ns on the started range; the p2 trace stays invisible
    r = client.get(
        "/v1/traces",
        params={"since": "2026-01-01T12:03:00Z", "until": "2026-01-01T12:06:00Z"},
    )
    assert _ids(r.json()["items"]) == {"b" * 32}

    r = client.get("/v1/traces", params={"min_duration_ms": 800})
    assert _ids(r.json()["items"]) == {"a" * 32}

    r = client.get("/v1/traces", params={"max_duration_ms": 400})
    assert _ids(r.json()["items"]) == {"b" * 32, "e" * 32}

    r = client.get("/v1/traces", params={"q": "web_search"})
    assert _ids(r.json()["items"]) == {"a" * 32}

    r = client.get("/v1/traces", params={"tool": "calc%"})
    assert _ids(r.json()["items"]) == {"b" * 32}

    # combined params AND together
    r = client.get(
        "/v1/traces",
        params={
            "session_id": "sess-1",
            "status": "error",
            "min_duration_ms": 500,
        },
    )
    assert _ids(r.json()["items"]) == {"a" * 32}

    # all items belong to the resolved project (p2 never leaks)
    r = client.get("/v1/traces")
    assert len(r.json()["items"]) == 5
    r = client.get("/v1/traces", params={"project_id": "p2"})
    assert _ids(r.json()["items"]) == {"d" * 32}


def test_api_traces_param_validation_422(client: TestClient) -> None:
    for params in (
        {"since": "not-a-date"},
        {"since": "2026-13-40T99:99:99"},
        {"until": "yesterday"},
        {"status": "bogus"},
        {"status": ""},
        {"min_duration_ms": "abc"},
        {"min_duration_ms": -1},
        {"min_duration_ms": "nan"},
        {"max_duration_ms": "inf"},
        {"limit": "notanint"},
    ):
        r = client.get("/v1/traces", params=params)
        assert r.status_code == 422, f"{params} → {r.status_code}: {r.text}"


def test_api_traces_valid_params_not_422(client: TestClient) -> None:
    r = client.get(
        "/v1/traces",
        params={
            "status": "ok",
            "session_id": "sess-2",
            "since": "2026-01-01T00:00:00+00:00",
            "until": "2027-01-01T00:00:00Z",
            "min_duration_ms": 0,
            "max_duration_ms": 99999.5,
            "q": "calc",
            "tool": "web_search",
            "limit": 10,
            "offset": 0,
        },
    )
    assert r.status_code == 200, r.text


# -- API: GET /v1/sessions ----------------------------------------------------


def test_api_sessions_response(client: TestClient) -> None:
    r = client.get("/v1/sessions")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"items", "limit", "offset"}
    items = body["items"]
    # newest activity first; tie-break session_id ASC (deterministic)
    assert [s["session_id"] for s in items] == ["sess-3", "sess-2", "sess-1"]
    for s in items:
        assert set(s) == {
            "session_id",
            "traces",
            "spans",
            "first_started_at",
            "last_activity_at",
        }
        assert isinstance(s["traces"], int)
        assert isinstance(s["spans"], int)
    assert items[-1]["last_activity_at"] == (
        T0 + timedelta(minutes=5, milliseconds=50)
    ).isoformat()
    # limit caps like every other list route
    assert client.get("/v1/sessions", params={"limit": 9999}).json()[
        "limit"
    ] == 500
    page = client.get("/v1/sessions", params={"limit": 2}).json()["items"]
    assert [s["session_id"] for s in page] == ["sess-3", "sess-2"]


def test_sessions_scoped_key_tenancy(tmp_path: Path) -> None:
    """keys mode: a p1-scoped reader sees p1 sessions only — p2 asks 403,
    and an omitted project_id resolves to cfg.project_id (never a silent
    cross-tenant roll-up)."""
    from forge_lens.security.keys import generate_key

    s = SQLiteTraceStore(tmp_path / "k.db")
    s.upsert_trace(_trace("a" * 32, session_id="sess-p1", project_id="p1"))
    s.upsert_trace(_trace("e" * 32, session_id="sess-p2", project_id="p2"))
    cfg = LensConfig(
        data_dir=tmp_path, db_path=tmp_path / "k.db", auth_mode="keys"
    )
    app = create_app(store=s, config=cfg)
    kid, secret, h = generate_key()
    s.create_api_key(kid, name="root", key_hash=h, role="admin")
    try:
        with TestClient(app) as c:
            admin = {"X-Forge-Lens-Key": secret}
            r = c.post(
                "/v1/admin/keys",
                headers=admin,
                json={"name": "p1-reader", "role": "reader",
                      "projects": ["p1"]},
            )
            p1 = {"X-Forge-Lens-Key": r.json()["secret"]}
            # unauthenticated reads are closed in keys mode
            assert c.get("/v1/sessions").status_code == 401
            # in-scope read returns exactly p1's sessions
            r = c.get("/v1/sessions", params={"project_id": "p1"}, headers=p1)
            assert r.status_code == 200, r.text
            assert [i["session_id"] for i in r.json()["items"]] == ["sess-p1"]
            # p2 is outside scope — denied, not silently filtered
            assert (
                c.get("/v1/sessions", params={"project_id": "p2"},
                      headers=p1).status_code
                == 403
            )
            # omitted pid resolves 'default' — also outside p1 scope
            assert c.get("/v1/sessions", headers=p1).status_code == 403
            # the unscoped admin can still name any project
            r = c.get(
                "/v1/sessions", params={"project_id": "p2"}, headers=admin
            )
            assert [i["session_id"] for i in r.json()["items"]] == ["sess-p2"]
    finally:
        s.close()


# -- CLI: traces list passthrough ---------------------------------------------


def test_cli_traces_list_filter_options(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from forge_lens.cli.main import app

    db = tmp_path / "cli.db"
    store = SQLiteTraceStore(db)
    _seed(store)
    store.close()

    runner = CliRunner()
    r = runner.invoke(
        app, ["traces", "list", "--db", str(db), "--status", "error"]
    )
    assert r.exit_code == 0, r.output
    assert "a" * 32 in r.output
    assert "b" * 32 not in r.output

    r = runner.invoke(
        app,
        ["traces", "list", "--db", str(db), "--tool", "calc%",
         "--since", "2026-01-01T00:00:00Z",
         "--max-duration-ms", "100", "--json"],
    )
    assert r.exit_code == 0, r.output
    import json

    items = json.loads(r.output)
    assert [i["trace_id"] for i in items] == ["b" * 32]

    # malformed --since is a usage error (exit 2), never a store query
    r = runner.invoke(
        app, ["traces", "list", "--db", str(db), "--since", "bogus"]
    )
    assert r.exit_code == 2
    r = runner.invoke(
        app, ["traces", "list", "--db", str(db), "--min-duration-ms", "-5"]
    )
    assert r.exit_code == 2


def test_cli_traces_list_session_filter(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from forge_lens.cli.main import app

    db = tmp_path / "cli2.db"
    store = SQLiteTraceStore(db)
    _seed(store)
    store.close()

    r = CliRunner().invoke(
        app, ["traces", "list", "--db", str(db), "--session", "sess-1",
              "--json"]
    )
    import json

    assert r.exit_code == 0, r.output
    assert {i["trace_id"] for i in json.loads(r.output)} == {
        "a" * 32,
        "b" * 32,
    }


# -- store + API: list_tools ---------------------------------------------------


def test_list_tools_rollup(store: SQLiteTraceStore) -> None:
    """Per-name calls/errors/traces; unnamed tool spans are bucketed."""
    tools = store.list_tools("default")
    by_name = {t["tool"]: t for t in tools}
    # seeded 'default' traces: web_search (1 err), calc% (1 ok),
    # calculator (1 ok), plus two TOOL spans with no tool.name attr
    assert by_name["web_search"]["errors"] == 1
    assert by_name["web_search"]["calls"] == 1
    assert by_name["calc%"]["calls"] == 1
    assert by_name["calculator"]["calls"] == 1
    assert by_name["(unnamed)"]["calls"] == 2
    for row in tools:
        assert row["calls"] >= 1
        assert row["errors"] <= row["calls"]
        assert row["traces"] >= 1
        assert row["avg_duration_ms"] is None or row["avg_duration_ms"] >= 0


def test_list_tools_project_isolation_and_wildcard(
    store: SQLiteTraceStore,
) -> None:
    default = {t["tool"] for t in store.list_tools("default")}
    p2 = {t["tool"] for t in store.list_tools("p2")}
    all_tools = {t["tool"] for t in store.list_tools(None)}
    assert p2 == {"(unnamed)"}
    assert all_tools == default | p2


def test_tools_endpoint_shape(client) -> None:
    r = client.get("/v1/tools")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"items", "limit", "offset"}
    for row in body["items"]:
        assert set(row) == {
            "tool", "calls", "traces", "errors", "avg_duration_ms",
        }


def test_tools_cli_renders_rollup(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from forge_lens.cli.main import app

    s = SQLiteTraceStore(tmp_path / "cli.db")
    _seed(s)
    s.close()
    r = CliRunner().invoke(
        app, ["tools", "--db", str(tmp_path / "cli.db"), "--project", "default"]
    )
    assert r.exit_code == 0
    assert "web_search" in r.output
    assert "calls=4" not in r.output  # seeded counts are 1/1/1 + 2 unnamed
    assert "(unnamed)" in r.output
