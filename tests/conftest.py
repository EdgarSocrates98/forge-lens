"""Shared fixtures for Forge Lens tests."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from forge_lens.contracts.v1.trace import Span, SpanKind, Status, StatusCode, Trace, TraceSource
from forge_lens.storage.sqlite import SQLiteTraceStore

FIXTURES = Path(__file__).parent / "fixtures"


def pytest_addoption(parser: pytest.Parser) -> None:
    # Registered here, not in tests/bench/conftest.py: options must come
    # from a conftest loaded during CLI preparse — an ancestor of the
    # testpaths arg — so `--bench-report` parses on any invocation.
    parser.addoption(
        "--bench-report",
        action="store",
        default=None,
        metavar="PATH",
        help="write the tests/bench timing summary as JSON to PATH",
    )


@pytest.fixture
def tmp_store(tmp_path: Path) -> SQLiteTraceStore:
    store = SQLiteTraceStore(tmp_path / "lens.db")
    yield store  # type: ignore[misc]
    store.close()


@pytest.fixture
def synthetic_trace() -> Trace:
    """A deterministic small trace: agent → llm + tool."""
    tid = "a" * 32
    t0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    agent = Span(
        trace_id=tid,
        span_id="1" * 16,
        parent_span_id=None,
        name="agent.run",
        kind=SpanKind.AGENT,
        start_time=t0,
        end_time=t0 + timedelta(milliseconds=900),
        status=Status(code=StatusCode.OK),
    )
    llm = Span(
        trace_id=tid,
        span_id="2" * 16,
        parent_span_id="1" * 16,
        name="llm gpt-test",
        kind=SpanKind.LLM,
        start_time=t0 + timedelta(milliseconds=50),
        end_time=t0 + timedelta(milliseconds=400),
        attributes={
            "llm.model_name": "gpt-test",
            "forge_lens.token_usage": {
                "provider": "openai",
                "model": "gpt-test",
                "input_tokens": 100,
                "output_tokens": 42,
                "total_tokens": 142,
                "measurement_source": "provider",
            },
        },
        status=Status(code=StatusCode.OK),
    )
    tool = Span(
        trace_id=tid,
        span_id="3" * 16,
        parent_span_id="1" * 16,
        name="tool web_search",
        kind=SpanKind.TOOL,
        start_time=t0 + timedelta(milliseconds=420),
        end_time=t0 + timedelta(milliseconds=800),
        attributes={"tool.name": "web_search"},
        status=Status(code=StatusCode.ERROR, message="timeout"),
    )
    return Trace(
        trace_id=tid,
        root_span_id=agent.span_id,
        started_at=t0,
        ended_at=t0 + timedelta(milliseconds=900),
        status=Status(code=StatusCode.ERROR),
        source=TraceSource(system="pytest", sdk_name="forge-lens-sdk-python"),
        spans=[agent, llm, tool],
    )


def make_otlp_payload() -> dict[str, Any]:
    """Minimal OTLP/HTTP JSON ExportTraceServiceRequest."""
    base_ns = 1_767_264_000_000_000_000
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "otlp-test-svc"}},
                        {"key": "telemetry.sdk.language", "value": {"stringValue": "python"}},
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "test-lib", "version": "0.0.1"},
                        "spans": [
                            {
                                "traceId": "b" * 32,
                                "spanId": "d" * 16,
                                "name": "root-op",
                                "kind": 2,
                                "startTimeUnixNano": str(base_ns),
                                "endTimeUnixNano": str(base_ns + 500_000_000),
                                "attributes": [
                                    {
                                        "key": "openinference.span.kind",
                                        "value": {"stringValue": "AGENT"},
                                    }
                                ],
                                "status": {"code": 1},
                            },
                            {
                                "traceId": "b" * 32,
                                "spanId": "e" * 16,
                                "parentSpanId": "d" * 16,
                                "name": "llm-call",
                                "kind": 1,
                                "startTimeUnixNano": str(base_ns + 10_000_000),
                                "endTimeUnixNano": str(base_ns + 200_000_000),
                                "attributes": [
                                    {
                                        "key": "openinference.span.kind",
                                        "value": {"stringValue": "LLM"},
                                    },
                                    {
                                        "key": "llm.token_count.prompt",
                                        "value": {"intValue": "512"},
                                    },
                                    {
                                        "key": "llm.token_count.completion",
                                        "value": {"intValue": 64},
                                    },
                                    {
                                        "key": "llm.model_name",
                                        "value": {"stringValue": "claude-test"},
                                    },
                                ],
                                "status": {"code": 1},
                            },
                        ],
                    }
                ],
            }
        ]
    }


@pytest.fixture
def otlp_payload() -> dict[str, Any]:
    return make_otlp_payload()


@pytest.fixture
def fixtures_dir() -> Path:
    FIXTURES.mkdir(exist_ok=True)
    return FIXTURES


# --- offline gate ------------------------------------------------------------
# Sibling forges hard-block sockets across the test session. Forge Lens
# keeps the same guarantee scoped to the layers that must stay offline:
# unit and contract tests. Integration/e2e suites legitimately open
# loopback sockets (uvicorn in ``test_forge_e2e``/``test_full_flow``),
# and ``tests/unit/test_query_client.py`` is marked ``network`` for the
# same reason — it exists to prove the SDK over a real socket.

_OFFLINE_DIRS = {"unit", "contract"}


@pytest.fixture(autouse=True)
def _no_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any *new* socket inside unit/contract tests is a hard failure.

    Fresh ``socket()`` construction and every DNS/connect entrypoint
    raise; wrapping an existing fd stays allowed so in-process machinery
    that is not network I/O (``socketpair`` self-pipes behind anyio's
    blocking portal — the FastAPI ``TestClient`` path) keeps working.
    Tests that genuinely need sockets carry ``@pytest.mark.network``.
    """
    if not set(request.path.parts) & _OFFLINE_DIRS:
        return
    if request.node.get_closest_marker("network"):
        return

    real_socket = socket.socket

    class _GuardedSocket(real_socket):  # type: ignore[misc]
        def __init__(
            self, family: int = -1, type: int = -1, proto: int = -1,
            fileno: int | None = None,
        ) -> None:
            if fileno is None:
                raise AssertionError(
                    "network access attempted during test: socket()"
                )
            super().__init__(family, type, proto, fileno)

    def _blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted during test")

    monkeypatch.setattr(socket, "socket", _GuardedSocket)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
    monkeypatch.setattr(socket, "gethostbyname", _blocked)
    monkeypatch.setattr(socket, "gethostbyaddr", _blocked)
    monkeypatch.setattr(socket, "getnameinfo", _blocked)
