from __future__ import annotations

import pytest

from forge_lens.contracts.v1.trace import SpanKind, StatusCode
from forge_lens.core.errors import ValidationError
from forge_lens.otlp.normalize import normalize_otlp_request


def test_otlp_normalize_groups_spans(otlp_payload: dict) -> None:
    traces = normalize_otlp_request(otlp_payload, project_id="p1")
    assert len(traces) == 1
    trace = traces[0]
    assert trace.trace_id == "b" * 32
    assert trace.project_id == "p1"
    assert len(trace.spans) == 2
    assert trace.source.system == "otlp-test-svc"
    assert trace.source.sdk_name == "test-lib"
    assert trace.root_span_id == "d" * 16
    assert trace.status.code == StatusCode.OK


def test_otlp_openinference_kind_and_tokens(otlp_payload: dict) -> None:
    trace = normalize_otlp_request(otlp_payload)[0]
    root, llm = trace.spans
    assert root.kind == SpanKind.AGENT  # openinference kind wins over OTel kind=2
    assert llm.kind == SpanKind.LLM
    usage = llm.attributes["forge_lens.token_usage"]
    assert usage["input_tokens"] == 512
    assert usage["output_tokens"] == 64
    assert usage["model"] == "claude-test"
    assert usage["measurement_source"] == "provider"


def test_otlp_missing_span_id_rejected() -> None:
    payload = {
        "resourceSpans": [
            {"scopeSpans": [{"spans": [{"traceId": "a" * 32, "name": "x"}]}]}
        ]
    }
    with pytest.raises(ValidationError):
        normalize_otlp_request(payload)


def test_otlp_empty_resource_spans() -> None:
    assert normalize_otlp_request({"resourceSpans": []}) == []
    assert normalize_otlp_request({}) == []


def test_otlp_bad_intvalue_raises_in_strict_mode() -> None:
    """Malformed primitives are a controlled ValidationError, never a 500."""
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "b" * 32,
                                "spanId": "d" * 16,
                                "name": "x",
                                "startTimeUnixNano": "1767264000000000000",
                                "attributes": [
                                    {"key": "n", "value": {"intValue": "junk"}}
                                ],
                            }
                        ]
                    }
                ]
            }
        ]
    }
    with pytest.raises(ValidationError, match="intValue"):
        normalize_otlp_request(payload)


def test_otlp_bad_timestamp_raises_in_strict_mode() -> None:
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "traceId": "b" * 32,
                                "spanId": "d" * 16,
                                "name": "x",
                                "startTimeUnixNano": "notnano",
                            }
                        ]
                    }
                ]
            }
        ]
    }
    with pytest.raises(ValidationError, match="timestamp"):
        normalize_otlp_request(payload)


def test_otlp_quarantine_keeps_valid_spans(otlp_payload: dict) -> None:
    """One malformed span must not reject the whole batch."""
    otlp_payload["resourceSpans"][0]["scopeSpans"][0]["spans"].append(
        {
            "traceId": "b" * 32,
            "spanId": "f" * 16,
            "name": "broken",
            "startTimeUnixNano": "notnano",
        }
    )
    errors: list[str] = []
    traces = normalize_otlp_request(otlp_payload, project_id="p1", errors=errors)
    assert len(traces) == 1
    assert len(traces[0].spans) == 2  # two good spans survived
    assert len(errors) == 1
    assert "quarantined" in errors[0]


def test_otlp_quarantine_reported_for_missing_ids() -> None:
    payload = {
        "resourceSpans": [
            {"scopeSpans": [{"spans": [{"traceId": "a" * 32, "name": "x"}]}]}
        ]
    }
    errors: list[str] = []
    traces = normalize_otlp_request(payload, errors=errors)
    assert traces == []
    assert len(errors) == 1
    assert "spanId" in errors[0]
