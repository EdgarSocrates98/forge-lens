"""Deterministic failure classification over stored span evidence.

Categories are ordered by specificity — the first matching rule wins,
and every report names the spans/attributes that triggered it. Every
category is gated on *recorded* evidence: HTTP status codes in span
attributes, ``exception.type`` strings in attributes or OTel exception
events, retry counters, boolean flags, or keywords in status/event
text. Nothing is guessed — a category only fires on what was captured.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.v1.trace import (
    IntegrityFlag,
    Span,
    SpanKind,
    StatusCode,
    Trace,
)
from forge_lens.graph.builder import cycle_member_ids

Confidence = Literal["observed", "inferred"]
"""Confidence in a classification or hypothesis: ``observed`` = direct
recorded span evidence (status code, exception type, attribute, flag);
``inferred`` = heuristic/structural or weak free-text signal only."""


class FailureCategory(StrEnum):
    """§05.3 taxonomy — every spec category plus observed extras."""

    TIMEOUT = "timeout"
    """Deadline/timeout signals in status message, attributes or events."""

    RATE_LIMIT = "rate_limit"
    """HTTP 429 / rate-limit evidence on a span that errored."""

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

    INVALID_ARGUMENTS = "invalid_arguments"
    """400/422, ValidationError family, or invalid/missing-argument text."""

    SCHEMA_MISMATCH = "schema_mismatch"
    """Schema/structured-output parse evidence on an errored span."""

    AUTH_ERROR = "auth_error"
    """401 / authentication / invalid-credential evidence."""

    MISSING_PERMISSIONS = "missing_permissions"
    """403 / forbidden / permission-denied evidence."""

    RETRY_EXHAUSTION = "retry_exhaustion"
    """Recorded retry counters at/over the configured or assumed max."""

    AGENT_CANCELLED = "agent_cancelled"
    """CancelledError / cancelled-flag evidence — execution was cancelled."""

    BUDGET_EXCEEDED = "budget_exceeded"
    """Quota/budget exhaustion — a 429 naming quota is budget, not throttle."""

    UNKNOWN = "unknown"


class FailureReport(LensModel):
    """Evidence-linked classification — ``category: unknown`` is a
    valid, honest answer (we never fabricate a cause)."""

    trace_id: str
    failed: bool
    category: FailureCategory = FailureCategory.UNKNOWN
    confidence: Confidence = "observed"
    """``observed`` = direct span evidence; ``inferred`` = heuristic."""
    evidence: dict[str, Any] = Field(default_factory=dict)
    detail: str = ""


# --------------------------------------------------------------------------
# Signal extraction
#
# A signal is ``(label, strong)``: the label names what was observed and
# ``strong`` marks recorded facts (status codes, exception types, counters,
# flags) versus weak keyword matches in free text.
# --------------------------------------------------------------------------

Signal = tuple[str, bool]

# "timeout"/"timed out"/"deadline" — bare "exceeded" is deliberately
# excluded: "context length exceeded" / "budget exceeded" are not
# timeout signals
_TIMEOUT_RE = re.compile(r"timeout|timed?\s*out|deadline", re.IGNORECASE)
_TIMEOUT_TYPE_RE = re.compile(r"timeout|deadline", re.IGNORECASE)

_RATE_RE = re.compile(r"rate[\s_-]*limit|too\s*many\s*requests", re.IGNORECASE)

# account-level exhaustion, not a transient throttle — wins over
# rate_limit when quota/budget terms ride along with a 429
_BUDGET_RE = re.compile(
    r"budget\s*exceed|exceed\w*\s+(the\s+)?budget|insufficient[_\s-]*quota|"
    r"quota\s*exceed|insufficient\s*(balance|funds|credit)|"
    r"out\s*of\s*(credit|funds|balance)|credit\s*limit|spending\s*limit",
    re.IGNORECASE,
)
_BUDGET_FLAGS = (
    "budget.exceeded",
    "llm.budget.exceeded",
    "llm.budget_exceeded",
    "cost.budget_exceeded",
    "forge_lens.budget_exceeded",
    "token_budget_exceeded",
)

_AUTH_RE = re.compile(
    r"unauthori[sz]ed|unauthenticat|authentication\s*fail|auth\s*fail|"
    r"invalid\s*(api[_\s-]*key|token|credential)|"
    r"expired\s*(api[_\s-]*key|token|credential)|bad\s*credential|"
    r"access\s*token.*(invalid|expired)|\b401\b",
    re.IGNORECASE,
)
_AUTH_TYPE_RE = re.compile(
    r"auth|unauthori[sz]ed|unauthenticat|credential", re.IGNORECASE
)

_PERM_RE = re.compile(
    r"\bforbidden\b|permission\s*(denied|required)|access\s*denied|"
    r"insufficient\s*(permission|scope|privilege)|lacks?\s*permission|"
    r"not\s*permitted|\b403\b",
    re.IGNORECASE,
)
_PERM_TYPE_RE = re.compile(
    r"permission|forbidden|accessdenied|privilege", re.IGNORECASE
)

# schema/structured-output signals — checked before invalid_arguments:
# "schema validation failed" is the more specific read than a bare 400
_SCHEMA_RE = re.compile(
    r"schema|structured\s*output|response[_\s-]*format|json[_\s-]*mode|"
    r"invalid\s*json|json\s*decod|could\s*not\s*parse|failed\s*to\s*parse|"
    r"does\s*not\s*(match|conform)\s*.{0,40}(schema|format|contract)|"
    r"(output|response)\s*(schema|contract)\s*mismatch",
    re.IGNORECASE,
)
_SCHEMA_TYPE_RE = re.compile(
    r"schema|jsondecode|json\.decoder|parseerror|responseformat",
    re.IGNORECASE,
)

_ARG_RE = re.compile(
    r"invalid\s*(argument|arg\b|parameter|param\b|input)|"
    r"missing\s*required\s*(argument|parameter|param|field|arg)|"
    r"unexpected\s*(keyword\s+)?argument|got\s*an\s*unexpected|"
    r"bad\s*request|validation\s*(error|failed)|argument.*(invalid|missing)|"
    r"\b400\b|\b422\b",
    re.IGNORECASE,
)
_ARG_TYPE_RE = re.compile(
    r"validationerror|invalidargument|invalid\s*argument|badrequest|"
    r"bad\s*request|missingargument|typeerror|argumenterror",
    re.IGNORECASE,
)

RETRY_EXHAUSTION_THRESHOLD = 3
"""Default bound when no explicit retry max is recorded — >= this many
recorded attempts/counters on an errored span reads as exhaustion."""

_RETRY_COUNT_KEYS = (
    "retry.count",
    "retry.attempts",
    "retry.attempt",
    "http.retry_count",
    "http.retries",
    "gen_ai.request.retries",
    "retries",
    "attempt",
    "attempts",
)
_RETRY_MAX_KEYS = (
    "retry.max",
    "retry.max_attempts",
    "max_retries",
    "max_attempts",
)
_RETRY_FLAGS = ("retry.exhausted", "retries_exhausted", "retry.budget_exceeded")
_RETRY_EXH_RE = re.compile(
    r"max(imum)?\s*retr|retr(y|ies)\s*(exhausted|failed|limit|budget)|"
    r"all\s*retr|exceeded\s*retr|retr(y|ies)\s*exceeded|"
    r"giving\s*up|gave\s*up",
    re.IGNORECASE,
)
_RETRY_TYPE_RE = re.compile(r"retr", re.IGNORECASE)  # e.g. tenacity RetryError

_CANCEL_TYPE_RE = re.compile(r"cancel|abort", re.IGNORECASE)
_CANCEL_RE = re.compile(r"cancel|abort", re.IGNORECASE)
_CANCEL_FLAGS = (
    "cancelled",
    "canceled",
    "aborted",
    "agent.cancelled",
    "agent.canceled",
    "task.cancelled",
)

_HTTP_STATUS_KEYS = ("http.status_code", "http.response.status_code")

_TEXT_KEYS = (
    "error.message",
    "error.type",
    "exception.message",
    "exception.type",
    "http.status_text",
    "otel.status_description",
)
_EVENT_TEXT_KEYS = (*_TEXT_KEYS, "message", "msg", "error")


def _err_spans(trace: Trace) -> list[Span]:
    return [s for s in trace.spans if s.status.code.value == "error"]


def _http_status(span: Span) -> int | None:
    for key in _HTTP_STATUS_KEYS:
        v = span.attributes.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            return v
        if isinstance(v, str) and v.strip().isdigit():
            return int(v.strip())
    return None


def _num_attr(span: Span, key: str) -> int | None:
    v = span.attributes.get(key)
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def _flagged(span: Span, keys: tuple[str, ...]) -> str | None:
    """Name of the first truthy flag attribute, if any."""
    for key in keys:
        v = span.attributes.get(key)
        if v is True:
            return key
        if isinstance(v, str) and v.strip().lower() in {"true", "1", "yes"}:
            return key
    return None


def _exception_types(span: Span) -> list[str]:
    """``exception.type`` from span attributes and OTel exception events."""
    out: list[str] = []
    v = span.attributes.get("exception.type")
    if isinstance(v, str):
        out.append(v)
    for ev in span.events:
        t = ev.attributes.get("exception.type")
        if isinstance(t, str):
            out.append(t)
    return out


def _text(span: Span) -> str:
    """Free-text evidence: status message, error-ish attributes, event
    names and the exception/error attributes of recorded events."""
    parts = [span.status.message or ""]
    for key in _TEXT_KEYS:
        v = span.attributes.get(key)
        if isinstance(v, str):
            parts.append(v)
    for ev in span.events:
        parts.append(ev.name)
        for key in _EVENT_TEXT_KEYS:
            v = ev.attributes.get(key)
            if isinstance(v, str):
                parts.append(v)
    return " ".join(parts)


def _retry_count(span: Span) -> int | None:
    best: int | None = None
    for key in _RETRY_COUNT_KEYS:
        n = _num_attr(span, key)
        if n is not None:
            best = n if best is None else max(best, n)
    return best


def _retry_max(span: Span) -> int | None:
    for key in _RETRY_MAX_KEYS:
        n = _num_attr(span, key)
        if n is not None:
            return n
    return None


def _span_signals(span: Span) -> dict[FailureCategory, list[Signal]]:
    """Every category this span carries *recorded* evidence for."""
    sigs: dict[FailureCategory, list[Signal]] = {}

    def add(cat: FailureCategory, label: str, strong: bool) -> None:
        bucket = sigs.setdefault(cat, [])
        if (label, strong) not in bucket:
            bucket.append((label, strong))

    code = _http_status(span)
    text = _text(span)
    types = _exception_types(span)

    # budget/quota — before rate_limit: a 429 naming quota/budget is
    # account exhaustion, not a transient throttle
    key = _flagged(span, _BUDGET_FLAGS)
    if key:
        add(FailureCategory.BUDGET_EXCEEDED, f"attr:{key}=true", True)
    if _BUDGET_RE.search(text):
        if code == 429:
            add(FailureCategory.BUDGET_EXCEEDED,
                "http_status:429+quota-terms", True)
        else:
            add(FailureCategory.BUDGET_EXCEEDED,
                "message:budget-quota-terms", False)

    # authentication
    if code == 401:
        add(FailureCategory.AUTH_ERROR, "http_status:401", True)
    for t in types:
        if _AUTH_TYPE_RE.search(t):
            add(FailureCategory.AUTH_ERROR, f"exception_type:{t}", True)
    if _AUTH_RE.search(text):
        add(FailureCategory.AUTH_ERROR, "message:auth-terms", False)

    # permissions
    if code == 403:
        add(FailureCategory.MISSING_PERMISSIONS, "http_status:403", True)
    for t in types:
        if _PERM_TYPE_RE.search(t):
            add(FailureCategory.MISSING_PERMISSIONS,
                f"exception_type:{t}", True)
    if _PERM_RE.search(text):
        add(FailureCategory.MISSING_PERMISSIONS,
            "message:permission-terms", False)

    # schema mismatch — before invalid_arguments: schema/parse wording
    # is the more specific read of a validation failure
    for t in types:
        if _SCHEMA_TYPE_RE.search(t):
            add(FailureCategory.SCHEMA_MISMATCH, f"exception_type:{t}", True)
    if _SCHEMA_RE.search(text):
        add(FailureCategory.SCHEMA_MISMATCH, "message:schema-terms", False)

    # invalid arguments
    if code in (400, 422):
        add(FailureCategory.INVALID_ARGUMENTS, f"http_status:{code}", True)
    for t in types:
        if _ARG_TYPE_RE.search(t):
            add(FailureCategory.INVALID_ARGUMENTS,
                f"exception_type:{t}", True)
    if _ARG_RE.search(text):
        add(FailureCategory.INVALID_ARGUMENTS, "message:argument-terms",
            False)

    # retry exhaustion — recorded counters, explicit flags, retry events,
    # RetryError types, or exhaustion wording
    n = _retry_count(span)
    if n is not None:
        mx = _retry_max(span)
        if mx is not None and n >= mx:
            add(FailureCategory.RETRY_EXHAUSTION,
                f"attr:retries={n}>=max={mx}", True)
        elif n >= RETRY_EXHAUSTION_THRESHOLD:
            add(FailureCategory.RETRY_EXHAUSTION,
                f"attr:retries={n}>=threshold:{RETRY_EXHAUSTION_THRESHOLD}",
                True)
    key = _flagged(span, _RETRY_FLAGS)
    if key:
        add(FailureCategory.RETRY_EXHAUSTION, f"attr:{key}=true", True)
    retry_events = sum(1 for ev in span.events if "retr" in ev.name.lower())
    if retry_events >= RETRY_EXHAUSTION_THRESHOLD:
        add(FailureCategory.RETRY_EXHAUSTION,
            f"events:retry={retry_events}", True)
    for t in types:
        if _RETRY_TYPE_RE.search(t):
            add(FailureCategory.RETRY_EXHAUSTION, f"exception_type:{t}",
                True)
    if _RETRY_EXH_RE.search(text):
        add(FailureCategory.RETRY_EXHAUSTION,
            "message:retry-exhaustion-terms", False)

    # cancellation — strong: CancelledError types / cancelled flags;
    # weak: cancel wording in the status message alone
    for t in types:
        if _CANCEL_TYPE_RE.search(t):
            add(FailureCategory.AGENT_CANCELLED, f"exception_type:{t}",
                True)
    key = _flagged(span, _CANCEL_FLAGS)
    if key:
        add(FailureCategory.AGENT_CANCELLED, f"attr:{key}=true", True)
    if _CANCEL_RE.search(span.status.message or ""):
        add(FailureCategory.AGENT_CANCELLED,
            "status_message:cancelled", False)

    # rate limit — plain 429 or throttle wording on the span that errored
    if code == 429:
        add(FailureCategory.RATE_LIMIT, "http_status:429", True)
    if _RATE_RE.search(text):
        add(FailureCategory.RATE_LIMIT, "message:rate-limit-terms", False)

    # timeout — type evidence is strong, free-text wording is weak
    for t in types:
        if _TIMEOUT_TYPE_RE.search(t):
            add(FailureCategory.TIMEOUT, f"exception_type:{t}", True)
    if _TIMEOUT_RE.search(text):
        add(FailureCategory.TIMEOUT, "message:timeout-terms", False)

    return sigs


# specificity order for signal categories — the first matched wins.
# budget precedes rate_limit (quota terms beat a bare 429); auth/
# permissions precede the argument family; cancellation sits after the
# cause categories because cancel is often how a deadline/budget
# *manifests* — competing categories remain visible in hypotheses.
_PRIORITY = (
    FailureCategory.BUDGET_EXCEEDED,
    FailureCategory.AUTH_ERROR,
    FailureCategory.MISSING_PERMISSIONS,
    FailureCategory.SCHEMA_MISMATCH,
    FailureCategory.INVALID_ARGUMENTS,
    FailureCategory.RETRY_EXHAUSTION,
    FailureCategory.RATE_LIMIT,
    FailureCategory.TIMEOUT,
    FailureCategory.AGENT_CANCELLED,
)


@dataclass
class _Scan:
    """One deterministic pass over a trace — shared by classification
    and hypothesis building so both read identical evidence."""

    errs: list[Span]
    unclosed: list[Span]
    cyclic: list[str]
    truncated: bool
    signals: dict[str, dict[FailureCategory, list[Signal]]]
    matched: dict[FailureCategory, list[tuple[Span, list[Signal]]]]
    cancelled: list[tuple[Span, list[Signal]]]


def _scan_trace(trace: Trace) -> _Scan:
    errs = _err_spans(trace)
    unclosed = [
        s for s in trace.spans if IntegrityFlag.MISSING_END in s.integrity_flags
    ]
    cyclic = sorted(
        cycle_member_ids({s.span_id: s.parent_span_id for s in trace.spans})
    )
    # an unclosed span in a trace that still declares itself open
    # (ended_at=None) is in-flight, not a failure — truncation is only
    # evidence when the envelope claims the run ended but spans didn't
    truncated = bool(unclosed) and trace.ended_at is not None
    signals = {s.span_id: _span_signals(s) for s in trace.spans}
    matched: dict[FailureCategory, list[tuple[Span, list[Signal]]]] = {}
    for s in errs:
        for cat, sigs in signals[s.span_id].items():
            matched.setdefault(cat, []).append((s, sigs))
    # cancellation on a span that did NOT record an error still counts —
    # but only on strong signals (exception type / flag): a status text
    # like "order was cancelled" is business data, not a cancelled run
    cancelled: list[tuple[Span, list[Signal]]] = []
    for s in trace.spans:
        if s.status.code is StatusCode.ERROR:
            continue
        strong = [
            sig
            for sig in signals[s.span_id].get(FailureCategory.AGENT_CANCELLED, [])
            if sig[1]
        ]
        if strong:
            cancelled.append((s, strong))
    return _Scan(
        errs=errs,
        unclosed=unclosed,
        cyclic=cyclic,
        truncated=truncated,
        signals=signals,
        matched=matched,
        cancelled=cancelled,
    )


def _signal_evidence(hits: list[tuple[Span, list[Signal]]]) -> dict[str, Any]:
    ordered = sorted(hits, key=lambda h: h[0].span_id)
    return {
        "span_ids": [s.span_id for s, _ in ordered],
        "signals": {
            s.span_id: [label for label, _ in sigs] for s, sigs in ordered
        },
        "names": sorted({s.name for s, _ in ordered}),
    }


def classify_failure(trace: Trace) -> FailureReport:
    """Classify a trace's failure from observed span evidence.

    Rules run in specificity order; the first match wins. ``failed`` is
    True only when work errored, never completed, was cancelled, or the
    declared topology is impossible — data-quality flags alone don't fail.
    """
    scan = _scan_trace(trace)
    # a trace "failed" when work errored, was truncated despite a closed
    # envelope, its own declared topology is impossible, or the run was
    # recorded as cancelled — data-quality flags (clock_skew, duplicate,
    # …) stay evidence, not failure verdicts
    report = FailureReport(
        trace_id=trace.trace_id,
        failed=bool(scan.errs)
        or scan.truncated
        or bool(scan.cyclic)
        or bool(scan.cancelled),
    )

    # parent-pointer cycle — any member of a real cycle in the stored
    # topology (self-loop or multi-node), via the same O(n) walk the
    # graph builder uses
    if scan.cyclic:
        report.category = FailureCategory.CYCLE
        report.evidence = {"span_ids": scan.cyclic}
        report.detail = (
            "parent-pointer cycle in stored topology — corrupted parentage"
        )
        return report

    # signal categories in specificity order — budget/quota before a bare
    # 429, auth before permissions, schema before generic invalid args,
    # cancellation last (it is often how another cause manifests)
    for cat in _PRIORITY:
        hits = scan.matched.get(cat)
        if hits:
            report.category = cat
            report.evidence = _signal_evidence(hits)
            report.detail = f"{cat.value}: {report.evidence['names']}"
            # compat keys kept for existing consumers (golden corpus,
            # CLI output): the first hit carries them
            if cat is FailureCategory.RATE_LIMIT:
                report.evidence["status"] = _http_status(hits[0][0])
            elif cat is FailureCategory.TIMEOUT:
                report.evidence["message"] = hits[0][0].status.message
            return report

    # kind-specific errors, most specific first
    for kind, cat in (
        (SpanKind.GUARDRAIL, FailureCategory.GUARDRAIL),
        (SpanKind.TOOL, FailureCategory.TOOL_ERROR),
        (SpanKind.LLM, FailureCategory.LLM_ERROR),
    ):
        hit = [s for s in scan.errs if s.kind is kind]
        if hit:
            report.category = cat
            report.evidence = {
                "span_ids": [s.span_id for s in hit],
                "messages": [s.status.message for s in hit],
                "names": [s.name for s in hit],
            }
            report.detail = f"{cat.value}: {[s.name for s in hit]}"
            return report

    # cancelled but never errored — the run was stopped, not failed by
    # its own work. Only strong signals (exception type, flags) reach
    # here; weak text mentions stay business data.
    if scan.cancelled:
        report.category = FailureCategory.AGENT_CANCELLED
        report.evidence = _signal_evidence(scan.cancelled)
        report.detail = (
            f"{FailureCategory.AGENT_CANCELLED.value}: "
            f"{report.evidence['names']} — cancellation recorded on "
            "non-errored span(s)"
        )
        return report

    # unclosed spans in a closed envelope — execution truncated
    # mid-flight (crash/timeout of the host process itself). An open
    # envelope with open spans is in-flight, not this category.
    if scan.truncated and not scan.errs:
        report.category = FailureCategory.INCOMPLETE
        report.confidence = "inferred"
        report.evidence = {
            "span_ids": [s.span_id for s in scan.unclosed],
            "count": len(scan.unclosed),
        }
        report.detail = (
            f"{len(scan.unclosed)} span(s) never ended — host likely "
            "died mid-run"
        )
        return report

    if scan.errs:
        report.evidence = {
            "span_ids": [s.span_id for s in scan.errs],
            "messages": [s.status.message for s in scan.errs],
        }
        report.detail = "error spans carry no recognizable signature"
        report.confidence = "inferred"
    return report
