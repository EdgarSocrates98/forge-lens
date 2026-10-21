"""Normalize OTLP/HTTP JSON (ExportTraceServiceRequest) into v1 contracts.

The parser is deliberately tolerant: OTLP allows unix-nanos as strings
or ints, attribute values in several shapes, and missing sections. What
cannot be interpreted is never dropped silently — unrecoverable spans go
to quarantine (``errors`` list or strict ``ValidationError``), and data
that can't be stored leaves evidence: events without ``timeUnixNano``
count in ``attributes["_otlp.events_dropped"]``, and kvlist entries
with a non-string key count in ``"_otlp.dropped_kv_pairs"``.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from forge_lens.contracts.provenance import Provenance
from forge_lens.contracts.v1.trace import (
    Span,
    SpanEvent,
    SpanLink,
    Status,
    StatusCode,
    Trace,
    TraceSource,
)
from forge_lens.core.errors import ValidationError
from forge_lens.core.time import from_unix_nano
from forge_lens.otlp.openinference import extract_token_usage, map_span_kind

# AnyValue can nest arbitrarily (arrayValue/kvlistValue). Uncapped
# recursion turns a small hostile payload into RecursionError → HTTP 500.
_MAX_ANYVALUE_DEPTH = 32


def _attr_value(raw: Any, depth: int = 0) -> Any:
    """Flatten an OTLP AnyValue to a Python scalar/list."""
    if depth > _MAX_ANYVALUE_DEPTH:
        return "[DROPPED:depth-limit]"  # same marker as sanitize
    if not isinstance(raw, dict):
        return raw
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in raw:
            v = raw[key]
            if key == "intValue" and isinstance(v, str):
                try:
                    return int(v)
                except ValueError:
                    raise ValidationError(f"OTLP intValue is not an integer: {v!r:.80}") from None
            return v
    if "arrayValue" in raw:
        arr = raw["arrayValue"]
        return (
            [_attr_value(v, depth + 1) for v in arr.get("values", [])]
            if isinstance(arr, dict)
            else raw
        )
    if "kvlistValue" in raw:
        kvl = raw["kvlistValue"]
        if not isinstance(kvl, dict):
            return raw
        out: dict[Any, Any] = {}
        dropped = 0
        for kv in kvl.get("values", []):
            # key must be a string — an array/object key is unhashable
            # and would crash dict insertion (→ HTTP 500)
            if isinstance(kv, dict) and isinstance(kv.get("key"), str):
                out[kv["key"]] = _attr_value(kv.get("value"), depth + 1)
            else:
                dropped += 1
        if dropped:
            # same contract as "[TRUNCATED: N items dropped]" — never silent
            out["_otlp.dropped_kv_pairs"] = dropped
        return out
    if "bytesValue" in raw:
        return {"_bytes": len(str(raw["bytesValue"]))}
    return raw


def _attrs(raw_list: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if not isinstance(raw_list, list):
        return out
    for item in raw_list:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            out[item["key"]] = _attr_value(item.get("value"))
    return out


def _parse_time(value: Any) -> Any:
    if value is None:
        return None
    try:
        return from_unix_nano(int(value))
    except (ValueError, TypeError, OverflowError):
        raise ValidationError(f"OTLP timestamp is not unix-nanos: {value!r:.80}") from None


def _parse_status(raw: Any) -> Status:
    if not isinstance(raw, dict):
        return Status(code=StatusCode.UNSET)
    code = raw.get("code", 0)
    # OTLP: STATUS_CODE_UNSET=0, OK=1, ERROR=2 (strings also seen).
    mapping = {
        0: StatusCode.UNSET,
        1: StatusCode.OK,
        2: StatusCode.ERROR,
        "STATUS_CODE_UNSET": StatusCode.UNSET,
        "STATUS_CODE_OK": StatusCode.OK,
        "STATUS_CODE_ERROR": StatusCode.ERROR,
    }
    return Status(code=mapping.get(code, StatusCode.UNSET), message=raw.get("message") or None)


def _parse_span(
    raw: dict[str, Any],
    provenance: Provenance,
    *,
    errors: list[str] | None = None,
) -> Span:
    trace_id = raw.get("traceId")
    span_id = raw.get("spanId")
    if not trace_id or not span_id:
        raise ValidationError(f"OTLP span missing traceId/spanId: {raw!r:.200}")

    attributes = _attrs(raw.get("attributes"))
    token_usage = extract_token_usage(attributes)
    if token_usage is not None:
        attributes["forge_lens.token_usage"] = token_usage.model_dump(
            mode="json", exclude_none=True
        )

    # An event without timeUnixNano cannot be stored (SpanEvent.time is
    # required). The drop is never silent: the count travels with the
    # span as evidence, and quarantine mode also records a warning.
    events: list[SpanEvent] = []
    dropped_events = 0
    for ev in raw.get("events", []):
        if isinstance(ev, dict) and ev.get("timeUnixNano") is not None:
            events.append(
                SpanEvent(
                    name=str(ev.get("name", "")),
                    time=_parse_time(ev.get("timeUnixNano")),
                    attributes=_attrs(ev.get("attributes")),
                )
            )
        else:
            dropped_events += 1
    if dropped_events:
        attributes["_otlp.events_dropped"] = dropped_events
        if errors is not None:
            errors.append(
                f"span {span_id!r}: dropped {dropped_events} event(s) "
                "without timeUnixNano"
            )
    links = [
        SpanLink(
            trace_id=lk.get("traceId", ""),
            span_id=lk.get("spanId", ""),
            attributes=_attrs(lk.get("attributes")),
        )
        for lk in raw.get("links", [])
        if isinstance(lk, dict)
    ]

    raw_parent = raw.get("parentSpanId") or None
    if isinstance(raw_parent, str) and raw_parent.strip("0") == "":
        # all-zero parentSpanId means "no parent" — never a real parent
        raw_parent = None

    return Span(
        trace_id=str(trace_id),
        span_id=str(span_id),
        parent_span_id=str(raw_parent) if raw_parent is not None else None,
        name=str(raw.get("name", "unnamed")),
        kind=map_span_kind(attributes, raw.get("kind")),
        start_time=_parse_time(raw.get("startTimeUnixNano")),
        end_time=_parse_time(raw.get("endTimeUnixNano")),
        attributes=attributes,
        events=events,
        links=links,
        status=_parse_status(raw.get("status")),
        provenance=provenance,
    )


def normalize_otlp_request(
    payload: Any,
    *,
    project_id: str = "default",
    provenance: Provenance = Provenance.OBSERVED,
    errors: list[str] | None = None,
) -> list[Trace]:
    """Convert an ExportTraceServiceRequest dict into internal Traces.

    Spans are grouped per (resource, trace_id); a single request may
    legitimately carry spans from several traces and several resources.

    When ``errors`` is provided, malformed spans are quarantined — the
    reason is appended to ``errors`` and parsing continues. When
    ``errors`` is ``None`` the first malformed span raises (strict mode).
    """
    if not isinstance(payload, dict):
        raise ValidationError(
            f"OTLP payload must be a JSON object, got {type(payload).__name__}"
        )
    resource_spans = payload.get("resourceSpans") or payload.get("resource_spans") or []
    if not isinstance(resource_spans, list):
        raise ValidationError("resourceSpans must be a list")

    grouped: dict[tuple[int, str], Trace] = {}
    for rs_idx, rs in enumerate(resource_spans):
        if not isinstance(rs, dict):
            continue
        resource_attrs = _attrs((rs.get("resource") or {}).get("attributes"))
        scope_spans = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []
        for ss in scope_spans:
            if not isinstance(ss, dict):
                continue
            scope = ss.get("scope") or ss.get("instrumentationLibrary") or {}
            sdk_name = scope.get("name")
            sdk_version = scope.get("version")
            for raw_span in ss.get("spans", []):
                if not isinstance(raw_span, dict):
                    continue
                try:
                    span = _parse_span(raw_span, provenance, errors=errors)
                except (ValidationError, PydanticValidationError) as exc:
                    if errors is None:
                        raise
                    errors.append(
                        f"span {raw_span.get('spanId', '?')!r} quarantined: {exc}"
                    )
                    continue
                key = (rs_idx, span.trace_id)
                trace = grouped.get(key)
                if trace is None:
                    service = resource_attrs.get("service.name")
                    trace = Trace(
                        trace_id=span.trace_id,
                        project_id=project_id,
                        session_id=(
                            str(resource_attrs["session.id"])
                            if "session.id" in resource_attrs
                            else None
                        ),
                        started_at=span.start_time,
                        source=TraceSource(
                            system=str(service) if service else "otlp",
                            sdk_name=sdk_name,
                            sdk_version=sdk_version,
                            runtime=(
                                str(resource_attrs["telemetry.sdk.language"])
                                if "telemetry.sdk.language" in resource_attrs
                                else None
                            ),
                            language=(
                                str(resource_attrs["telemetry.sdk.language"])
                                if "telemetry.sdk.language" in resource_attrs
                                else None
                            ),
                        ),
                        resource_attributes=resource_attrs,
                        spans=[],
                        provenance=provenance,
                    )
                    grouped[key] = trace
                trace.spans.append(span)

    for trace in grouped.values():
        _finalize_trace(trace)
    return list(grouped.values())


def _finalize_trace(trace: Trace) -> None:
    """Derive root span, time bounds and status rollup deterministically."""
    if not trace.spans:
        return
    parent_ids = {s.parent_span_id for s in trace.spans if s.parent_span_id}
    span_ids = {s.span_id for s in trace.spans}
    roots = [s for s in trace.spans if s.parent_span_id is None or s.parent_span_id not in span_ids]
    if roots:
        roots.sort(key=lambda s: s.start_time)
        trace.root_span_id = roots[0].span_id
    elif parent_ids:
        # All parents missing → partial trace; earliest span is de-facto root.
        earliest = min(trace.spans, key=lambda s: s.start_time)
        trace.root_span_id = earliest.span_id

    trace.started_at = min(s.start_time for s in trace.spans)
    ended = [s.end_time for s in trace.spans if s.end_time is not None]
    trace.ended_at = max(ended) if ended else None

    if any(s.status.code == StatusCode.ERROR for s in trace.spans):
        trace.status = Status(code=StatusCode.ERROR)
    elif all(s.status.code == StatusCode.OK for s in trace.spans):
        trace.status = Status(code=StatusCode.OK)
    else:
        trace.status = Status(code=StatusCode.UNSET)
