from __future__ import annotations

from typing import Any

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


def _span_payload(**over: object) -> dict:
    base = {
        "traceId": "b" * 32,
        "spanId": "d" * 16,
        "name": "s",
        "startTimeUnixNano": "1767264000000000000",
    }
    base.update(over)
    return {
        "resourceSpans": [{"scopeSpans": [{"spans": [base]}]}]
    }


def test_otlp_enum_name_span_kind() -> None:
    """OTLP protobuf-JSON emits enum names: 'SPAN_KIND_SERVER' must map
    to SERVER, not silently fall back to internal."""
    cases = {
        "SPAN_KIND_INTERNAL": SpanKind.INTERNAL,
        "SPAN_KIND_SERVER": SpanKind.SERVER,
        "SPAN_KIND_CLIENT": SpanKind.CLIENT,
        "SPAN_KIND_PRODUCER": SpanKind.PRODUCER,
        "SPAN_KIND_CONSUMER": SpanKind.CONSUMER,
        "span_kind_client": SpanKind.CLIENT,  # case-tolerant
    }
    for raw_kind, expected in cases.items():
        trace = normalize_otlp_request(_span_payload(kind=raw_kind))[0]
        assert trace.spans[0].kind == expected, raw_kind
    # numeric kinds still work; unknown names degrade to internal
    assert (
        normalize_otlp_request(_span_payload(kind=3))[0].spans[0].kind
        == SpanKind.CLIENT
    )
    assert (
        normalize_otlp_request(_span_payload(kind="SOMETHING_ELSE"))[0]
        .spans[0].kind
        == SpanKind.INTERNAL
    )
    # OpenInference kind still wins over the OTel field
    payload = _span_payload(kind="SPAN_KIND_SERVER")
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"] = [
        {"key": "openinference.span.kind", "value": {"stringValue": "LLM"}}
    ]
    assert (
        normalize_otlp_request(payload)[0].spans[0].kind == SpanKind.LLM
    )


def test_otlp_all_zero_parent_span_id_is_absent() -> None:
    """An all-zero parentSpanId means 'no parent' (W3C) — it must not
    create a phantom MISSING_PARENT flag."""
    trace = normalize_otlp_request(
        _span_payload(parentSpanId="0000000000000000")
    )[0]
    span = trace.spans[0]
    assert span.parent_span_id is None
    assert not any(f.value == "missing_parent" for f in span.integrity_flags)
    # a real parent id still passes through
    trace2 = normalize_otlp_request(_span_payload(parentSpanId="c" * 16))[0]
    assert trace2.spans[0].parent_span_id == "c" * 16


def test_otlp_non_dict_payload_rejected() -> None:
    """A JSON array/string/number body raises ValidationError — never
    an AttributeError that would surface as HTTP 500."""
    for bad in ([1, 2, 3], "hello", 42, None):
        with pytest.raises(ValidationError):
            normalize_otlp_request(bad)


def test_otlp_event_without_time_is_evidence_not_silent() -> None:
    """Events lacking timeUnixNano can't be stored (time is required) —
    the drop is recorded on the span and in the quarantine list."""
    payload = _span_payload(
        events=[
            {"name": "ok", "timeUnixNano": "1767264001000000000"},
            {"name": "lost"},          # no timeUnixNano
            "not-even-a-dict",          # malformed
        ]
    )
    errors: list[str] = []
    trace = normalize_otlp_request(payload, errors=errors)[0]
    span = trace.spans[0]
    assert len(span.events) == 1
    assert span.events[0].name == "ok"
    # evidence travels with the data itself
    assert span.attributes["_otlp.events_dropped"] == 2
    # and is reported when a quarantine list is provided
    assert any("dropped 2 event" in e for e in errors)


def test_otlp_event_drop_recorded_in_strict_mode_too() -> None:
    """Strict mode (errors=None) still marks the span — evidence is
    data-level, not just a warning channel."""
    trace = normalize_otlp_request(
        _span_payload(events=[{"name": "lost"}])
    )[0]
    assert trace.spans[0].events == []
    assert trace.spans[0].attributes["_otlp.events_dropped"] == 1


def test_otlp_kvlist_nonstring_key_quarantined() -> None:
    """A kvlistValue entry whose `key` is an array/object is unhashable —
    it must not crash normalization (was a 500), malformed pairs skip."""
    payload = _span_payload(
        attributes=[
            {
                "key": "nested",
                "value": {
                    "kvlistValue": {
                        "values": [
                            {"key": [1, 2], "value": {"stringValue": "x"}},
                            {"key": {"a": 1}, "value": {"stringValue": "y"}},
                            {"key": "real", "value": {"stringValue": "ok"}},
                        ]
                    }
                },
            }
        ]
    )
    trace = normalize_otlp_request(payload)[0]
    assert trace.spans[0].attributes["nested"] == {
        "real": "ok",
        "_otlp.dropped_kv_pairs": 2,  # evidence — the drop is not silent
    }


def test_otlp_deeply_nested_anyvalue_does_not_crash() -> None:
    """Unbounded _attr_value recursion → RecursionError → HTTP 500.
    Deep nesting must hit the depth cap and leave a marker instead."""
    deep: dict[str, Any] = {"stringValue": "bottom"}
    for _ in range(2_000):
        deep = {"arrayValue": {"values": [deep]}}
    payload = _span_payload(attributes=[{"key": "deep", "value": deep}])
    trace = normalize_otlp_request(payload)[0]
    # parses without RecursionError; the marker proves the cap fired
    assert "[DROPPED:depth-limit]" in repr(trace.spans[0].attributes["deep"])


def test_otlp_bytesvalue_reports_decoded_length() -> None:
    """_bytes must count the decoded bytes, not the base64 characters."""
    import base64 as _b64

    raw = b"hello-world-123"  # 15 bytes -> 20 base64 chars
    payload = _span_payload(
        attributes=[
            {"key": "blob", "value": {"bytesValue": _b64.b64encode(raw).decode()}}
        ]
    )
    trace = normalize_otlp_request(payload)[0]
    assert trace.spans[0].attributes["blob"] == {"_bytes": 15}


_SPAN_BASE = {
    "traceId": "b" * 32,
    "spanId": "d" * 16,
    "name": "s",
    "startTimeUnixNano": "1767264000000000000",
}


def _wrap_spans(spans: list) -> dict:
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


def test_otlp_non_dict_resource_and_scope_not_500() -> None:
    """resource/scope as scalar used to AttributeError → HTTP 500.
    A malformed container must degrade, never crash."""
    for resource in ("x", 42, [1], True):
        errors: list[str] = []
        payload = {
            "resourceSpans": [
                {
                    "resource": resource,
                    "scopeSpans": [{"spans": [dict(_SPAN_BASE)]}],
                }
            ]
        }
        traces = normalize_otlp_request(payload, errors=errors)
        assert traces[0].spans[0].span_id == "d" * 16
        assert any("resource" in e for e in errors)
    for scope in ("x", 42, [1], True):
        errors = []
        payload = {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {"scope": scope, "spans": [dict(_SPAN_BASE)]}
                    ]
                }
            ]
        }
        traces = normalize_otlp_request(payload, errors=errors)
        assert traces[0].spans[0].span_id == "d" * 16
        assert any("scope" in e for e in errors)


def test_otlp_non_list_containers_not_500() -> None:
    """scopeSpans/spans/events/links as null-or-scalar used to TypeError
    → HTTP 500. Each must leave evidence, never crash."""
    for spans_field in (None, 5, "x"):
        errors: list[str] = []
        payload = {
            "resourceSpans": [{"scopeSpans": [{"spans": spans_field}]}]
        }
        traces = normalize_otlp_request(payload, errors=errors)
        if spans_field is not None:
            assert any("spans" in e for e in errors)
        assert traces == []

    for field in ("events", "links"):
        for bad_value in (None, 5, "x"):
            errors = []
            span = dict(_SPAN_BASE)
            span[field] = bad_value
            traces = normalize_otlp_request(_wrap_spans([span]), errors=errors)
            assert traces[0].spans[0].span_id == "d" * 16
            attrs = traces[0].spans[0].attributes
            if bad_value is None:
                # explicit null ≈ absent — same convention as
                # `resourceSpans or []` everywhere else in this parser
                assert "_otlp.malformed_fields" not in attrs
            else:
                assert field in attrs["_otlp.malformed_fields"]
                assert any(field in e for e in errors)


def test_otlp_non_list_field_strict_mode_raises() -> None:
    """Strict mode (errors=None) rejects a malformed list field."""
    span = dict(_SPAN_BASE)
    span["events"] = 5
    with pytest.raises(ValidationError):
        normalize_otlp_request(_wrap_spans([span]))


def test_otlp_anyvalue_non_list_values_not_500() -> None:
    """arrayValue/kvlistValue with non-list `values` used to TypeError —
    the malformed node must persist un-flattened as evidence."""
    cases = [
        ({"arrayValue": {"values": None}}, []),
        ({"arrayValue": {"values": 5}}, {"arrayValue": {"values": 5}}),
        ({"kvlistValue": {"values": None}}, {}),
        ({"kvlistValue": {"values": 5}}, {"kvlistValue": {"values": 5}}),
    ]
    for value, expected in cases:
        payload = _span_payload(attributes=[{"key": "a", "value": value}])
        trace = normalize_otlp_request(payload)[0]
        assert trace.spans[0].attributes["a"] == expected


def test_otlp_null_name_becomes_unnamed() -> None:
    """`name: null` must not stringify into the literal "None"."""
    trace = normalize_otlp_request(_span_payload(name=None))[0]
    assert trace.spans[0].name == "unnamed"


def test_otlp_runtime_uses_process_runtime_attrs() -> None:
    """runtime comes from process.runtime.*, not duplicated from
    telemetry.sdk.language — semconv-accurate, never the same string."""
    payload = {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "telemetry.sdk.language",
                            "value": {"stringValue": "python"},
                        },
                        {
                            "key": "process.runtime.name",
                            "value": {"stringValue": "cpython"},
                        },
                        {
                            "key": "process.runtime.version",
                            "value": {"stringValue": "3.12.1"},
                        },
                    ]
                },
                "scopeSpans": [{"spans": [dict(_SPAN_BASE)]}],
            }
        ]
    }
    trace = normalize_otlp_request(payload)[0]
    assert trace.source.language == "python"
    assert trace.source.runtime == "cpython-3.12.1"
    # name-only fallback
    payload["resourceSpans"][0]["resource"]["attributes"].pop()
    trace = normalize_otlp_request(payload)[0]
    assert trace.source.runtime == "cpython"
