"""Deterministic failure classification over stored span evidence.

Categories are ordered by specificity — the first matching rule wins,
and every report names the spans/attributes that triggered it.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.v1.trace import IntegrityFlag, Span, SpanKind, Trace


class FailureCategory(StrEnum):
    TIMEOUT = "timeout"
    """Deadline/timeout signals in status message or attributes."""

    RATE_LIMIT = "rate_limit"
    """HTTP 429 / rate-limit evidence."""

    TOOL_ERROR = "tool_error"
    """A tool-kind span ended in error."""

    LLM_ERROR = "llm_error"
    """An llm-kind span ended in error."""

    GUARDRAIL = "guardrail"
    """A guardrail-kind span flagged the execution."""

    INCOMPLETE = "incomplete"
    """Spans still running / unclosed — execution truncated mid-flight."""

    CYCLE = "cycle"
    """Parent-pointer cycle detected in the stored topology."""

    UNKNOWN = "unknown"


class FailureReport(LensModel):
    """Evidence-linked classification — ``category: unknown`` is a
    valid, honest answer (we never fabricate a cause)."""

    trace_id: str
    failed: bool
    category: FailureCategory = FailureCategory.UNKNOWN
    confidence: str = "observed"
    """``observed`` = direct span evidence; ``inferred`` = heuristic."""
    evidence: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


_TIMEOUT_RE = re.compile(r"timeout|timed?\s*out|deadline|exceeded", re.IGNORECASE)


def _err_spans(trace: Trace) -> list[Span]:
    return [s for s in trace.spans if s.status.code.value == "error"]


def _msg(span: Span) -> str:
    parts = [span.status.message or ""]
    for key in ("error.message", "exception.message", "http.status_text"):
        v = span.attributes.get(key)
        if isinstance(v, str):
            parts.append(v)
    return " ".join(parts)


def classify_failure(trace: Trace) -> FailureReport:
    """Classify a trace's failure from observed span evidence.

    Rules run in specificity order; the first match wins. A trace with
    no error spans and no integrity anomalies reports ``failed=False``.
    """
    report = FailureReport(
        trace_id=trace.trace_id,
        failed=bool(_err_spans(trace))
        or any(
            IntegrityFlag.CLOCK_SKEW in s.integrity_flags for s in trace.spans
        ),
    )
    errs = _err_spans(trace)

    # parent-cycle evidence: member flags emitted at ingest/graph time
    cyclic = [s.span_id for s in trace.spans if s.parent_span_id == s.span_id]
    if cyclic:
        report.category = FailureCategory.CYCLE
        report.evidence = {"span_ids": cyclic}
        report.detail = "span declares itself as parent — corrupted topology"
        return report

    # rate limit: 429 in attributes/status of any span
    for s in trace.spans:
        status_code = s.attributes.get("http.status_code") or s.attributes.get(
            "http.response.status_code"
        )
        if status_code == 429 or "rate limit" in _msg(s).lower():
            report.category = FailureCategory.RATE_LIMIT
            report.evidence = {"span_ids": [s.span_id], "status": status_code}
            report.detail = f"{s.name}: rate-limit signal observed"
            return report

    # timeout: error span whose message/attrs carry deadline evidence
    for s in errs:
        if _TIMEOUT_RE.search(_msg(s)):
            report.category = FailureCategory.TIMEOUT
            report.evidence = {
                "span_ids": [s.span_id],
                "message": s.status.message,
            }
            report.detail = f"{s.name}: timeout signal in status/attrs"
            return report

    # kind-specific errors, most specific first
    for kind, cat in (
        (SpanKind.GUARDRAIL, FailureCategory.GUARDRAIL),
        (SpanKind.TOOL, FailureCategory.TOOL_ERROR),
        (SpanKind.LLM, FailureCategory.LLM_ERROR),
    ):
        hit = [s for s in errs if s.kind is kind]
        if hit:
            report.category = cat
            report.evidence = {
                "span_ids": [s.span_id for s in hit],
                "messages": [s.status.message for s in hit],
            }
            report.detail = f"{cat.value}: {[s.name for s in hit]}"
            return report

    # unclosed spans — execution truncated mid-flight (crash/timeout of
    # the host process itself)
    unclosed = [
        s for s in trace.spans if IntegrityFlag.MISSING_END in s.integrity_flags
    ]
    if unclosed and not errs:
        report.category = FailureCategory.INCOMPLETE
        report.confidence = "inferred"
        report.evidence = {
            "span_ids": [s.span_id for s in unclosed],
            "count": len(unclosed),
        }
        report.detail = (
            f"{len(unclosed)} span(s) never ended — host likely died mid-run"
        )
        return report

    if errs:
        report.evidence = {
            "span_ids": [s.span_id for s in errs],
            "messages": [s.status.message for s in errs],
        }
        report.detail = "error spans carry no recognizable signature"
        report.confidence = "inferred"
    return report
