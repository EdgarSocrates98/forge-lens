"""Shared fixtures for Forge Lens tests."""

from __future__ import annotations

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
