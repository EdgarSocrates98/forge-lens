"""Cycle 05.3 / 05.4 / 12.7 — extended failure taxonomy, root-cause
hypotheses, and evaluation-scenario suggestions.

Every category is exercised against crafted evidence and — just as
importantly — against evidence that must *not* fire it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from forge_lens.contracts.v1.trace import (
    Span,
    SpanEvent,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.investigate import (
    FailureCategory,
    FailureReport,
    HypothesisStatus,
    build_hypotheses,
    classify_failure,
    suggest_evaluators,
)
from forge_lens.investigate.classify import RETRY_EXHAUSTION_THRESHOLD

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TRACE_ID = "f" * 32


def _span(
    sid: str,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    error: bool = True,
    msg: str | None = None,
    attrs: dict[str, Any] | None = None,
    events: list[SpanEvent] | None = None,
) -> Span:
    return Span(
        trace_id=TRACE_ID,
        span_id=sid.ljust(16, "0")[:16],
        name=name,
        kind=kind,
        start_time=T0,
        end_time=T0 + timedelta(milliseconds=100),
        attributes=attrs or {},
        events=events or [],
        status=Status(
            code=StatusCode.ERROR if error else StatusCode.OK,
            message=msg,
        ),
    )


def _trace(spans: list[Span], **kw: Any) -> Trace:
    return Trace(
        trace_id=str(kw.get("trace_id", TRACE_ID)),
        started_at=T0,
        ended_at=T0 + timedelta(seconds=10),
        spans=spans,
    )


def _exc_event(*exc_types: str, offset_ms: int = 10) -> list[SpanEvent]:
    """OTel-style exception events."""
    return [
        SpanEvent(
            name="exception",
            time=T0 + timedelta(milliseconds=offset_ms + i),
            attributes={"exception.type": t, "exception.message": "boom"},
        )
        for i, t in enumerate(exc_types)
    ]


# -- 05.3: new categories ----------------------------------------------------


def test_classify_auth_error_status_and_type() -> None:
    s = _span("a1", "llm call", kind=SpanKind.LLM,
              attrs={"http.status_code": 401})
    r = classify_failure(_trace([s]))
    assert r.failed
    assert r.category == "auth_error"
    assert r.evidence["span_ids"] == [s.span_id]
    assert "http_status:401" in r.evidence["signals"][s.span_id]

    s2 = _span("a1", "call", events=_exc_event("AuthenticationError"))
    assert classify_failure(_trace([s2])).category == "auth_error"


def test_classify_missing_permissions() -> None:
    s = _span("a1", "tool s3", kind=SpanKind.TOOL,
              attrs={"http.status_code": 403})
    assert classify_failure(_trace([s])).category == "missing_permissions"
    s2 = _span("a1", "tool", kind=SpanKind.TOOL,
               events=_exc_event("PermissionError"))
    assert classify_failure(_trace([s2])).category == "missing_permissions"


def test_classify_invalid_arguments() -> None:
    s = _span("a1", "tool fetch", kind=SpanKind.TOOL,
              attrs={"http.status_code": 400})
    r = classify_failure(_trace([s]))
    assert r.category == "invalid_arguments"
    s2 = _span("a1", "tool", kind=SpanKind.TOOL,
               events=_exc_event("ValidationError"))
    assert classify_failure(_trace([s2])).category == "invalid_arguments"
    s3 = _span("a1", "tool", kind=SpanKind.TOOL,
               attrs={"http.response.status_code": 422})
    assert classify_failure(_trace([s3])).category == "invalid_arguments"


def test_classify_schema_mismatch_beats_invalid_args() -> None:
    """Schema/parse wording is the more specific read than a bare 400."""
    s = _span("a1", "llm json", kind=SpanKind.LLM,
              attrs={"http.status_code": 400},
              events=_exc_event("SchemaValidationError"))
    assert classify_failure(_trace([s])).category == "schema_mismatch"
    s2 = _span("a1", "llm", kind=SpanKind.LLM,
               msg="failed to parse structured output")
    assert classify_failure(_trace([s2])).category == "schema_mismatch"


def test_classify_retry_exhaustion() -> None:
    s = _span("a1", "llm", kind=SpanKind.LLM, attrs={"retry.count": 5})
    r = classify_failure(_trace([s]))
    assert r.category == "retry_exhaustion"
    s2 = _span("a1", "llm", kind=SpanKind.LLM,
               attrs={"attempt": 3, "max_attempts": 3})
    assert classify_failure(_trace([s2])).category == "retry_exhaustion"
    s3 = _span("a1", "llm", kind=SpanKind.LLM,
               events=[SpanEvent(name="retry", time=T0)
                       for _ in range(RETRY_EXHAUSTION_THRESHOLD)])
    assert classify_failure(_trace([s3])).category == "retry_exhaustion"
    s4 = _span("a1", "llm", kind=SpanKind.LLM,
               msg="max retries exceeded, giving up")
    assert classify_failure(_trace([s4])).category == "retry_exhaustion"


def test_classify_agent_cancelled_err_and_nonerr() -> None:
    s = _span("a1", "agent", kind=SpanKind.AGENT,
              events=_exc_event("asyncio.CancelledError"))
    assert classify_failure(_trace([s])).category == "agent_cancelled"
    # a cancelled span that never errored still fails the run
    s2 = _span("a1", "agent", kind=SpanKind.AGENT, error=False,
               attrs={"agent.cancelled": True})
    r2 = classify_failure(_trace([s2]))
    assert r2.failed
    assert r2.category == "agent_cancelled"


def test_classify_budget_exceeded_beats_rate_limit() -> None:
    """429 naming quota is account exhaustion, not a transient throttle."""
    s = _span("a1", "llm", kind=SpanKind.LLM,
              attrs={"http.status_code": 429},
              msg="insufficient_quota: quota exceeded")
    assert classify_failure(_trace([s])).category == "budget_exceeded"
    s2 = _span("a1", "llm", kind=SpanKind.LLM,
               attrs={"budget.exceeded": True})
    assert classify_failure(_trace([s2])).category == "budget_exceeded"


def test_ordering_auth_beats_timeout_and_kind() -> None:
    """401 with deadline wording must land on auth, not timeout/kind."""
    s = _span("a1", "tool api", kind=SpanKind.TOOL,
              attrs={"http.status_code": 401},
              msg="deadline passed; unauthorized")
    r = classify_failure(_trace([s]))
    assert r.category == "auth_error"


def test_ordering_signal_beats_tool_kind() -> None:
    s = _span("a1", "tool api", kind=SpanKind.TOOL,
              attrs={"http.status_code": 422})
    assert classify_failure(_trace([s])).category == "invalid_arguments"


# -- 05.3: negative cases / regressions --------------------------------------


def test_business_cancel_text_not_a_failure() -> None:
    """'order was cancelled' on a healthy span is business data, not a
    cancelled run — weak text signals only count on errored spans."""
    ok = _span("a1", "tool orders", kind=SpanKind.TOOL, error=False,
               msg="order #42 was cancelled by customer")
    r = classify_failure(_trace([ok]))
    assert r.failed is False
    assert r.category == "unknown"


def test_existing_categories_unchanged() -> None:
    """Regression guards: prior taxonomy keeps its verdicts."""
    rl = _span("a1", "gen", kind=SpanKind.LLM,
               attrs={"http.status_code": 429}, msg="HTTP 429")
    assert classify_failure(_trace([rl])).category == "rate_limit"
    t = _span("a1", "call", kind=SpanKind.TOOL, msg="ETIMEDOUT")
    assert classify_failure(_trace([t])).category == "timeout"
    bare = _span("b1", "tool", kind=SpanKind.TOOL, msg="500 boom")
    assert classify_failure(_trace([bare])).category == "tool_error"
    ctx = _span("c1", "gen", kind=SpanKind.LLM,
                msg="context length exceeded")
    assert classify_failure(_trace([ctx])).category == "llm_error"


def test_unrecognized_error_stays_unknown() -> None:
    s = _span("a1", "work", msg="mysterious failure")
    r = classify_failure(_trace([s]))
    assert r.failed
    assert r.category == "unknown"
    assert r.confidence == "inferred"


# -- 05.4: hypotheses ----------------------------------------------------------


def test_hypothesis_carries_all_fields() -> None:
    s = _span("a1", "llm", kind=SpanKind.LLM,
              attrs={"http.status_code": 401},
              events=_exc_event("AuthenticationError"))
    hyps = build_hypotheses(_trace([s]))
    assert len(hyps) == 1
    h = hyps[0]
    assert h.hypothesis_id == f"{TRACE_ID}:auth_error"
    assert h.label == "auth_error"
    assert h.symptom
    assert h.supporting_evidence  # span_id + signal labels
    assert any("http_status:401" in e for e in h.supporting_evidence)
    assert h.missing_evidence  # gaps are named
    assert h.suggested_verification
    assert h.confidence == "observed"
    assert h.status == HypothesisStatus.SUPPORTED  # two strong signals


def test_hypothesis_proposed_on_single_weak_signal() -> None:
    s = _span("a1", "gen", kind=SpanKind.LLM, msg="deadline exceeded")
    hyps = build_hypotheses(_trace([s]))
    h = next(x for x in hyps if x.label == "timeout")
    assert h.status == HypothesisStatus.PROPOSED
    assert h.confidence == "inferred"
    assert h.missing_evidence  # thin evidence must name what's absent


def test_hypothesis_competing_signals_and_refutation() -> None:
    """A weak keyword drowned by a strong signal on the same span is
    refuted — with the competing evidence named."""
    s = _span("a1", "llm", kind=SpanKind.LLM,
              attrs={"http.status_code": 401},
              msg="unauthorized (timed out earlier)")
    hyps = build_hypotheses(_trace([s]))
    auth = next(x for x in hyps if x.label == "auth_error")
    timeout = next(x for x in hyps if x.label == "timeout")
    assert auth.status == HypothesisStatus.SUPPORTED
    assert timeout.status == HypothesisStatus.REFUTED
    assert timeout.contradictory_evidence
    assert any("401" in e for e in timeout.contradictory_evidence)


def test_hypothesis_unverifiable_for_unknown_failure() -> None:
    s = _span("a1", "work", msg="mystery")
    hyps = build_hypotheses(_trace([s]))
    assert len(hyps) == 1
    assert hyps[0].label == "unknown"
    assert hyps[0].status == HypothesisStatus.UNVERIFIABLE
    assert hyps[0].missing_evidence


def test_no_hypotheses_when_healthy() -> None:
    ok = _span("a1", "fine", error=False)
    assert build_hypotheses(_trace([ok])) == []


def test_hypothesis_deterministic_ids_and_cap() -> None:
    s = _span("a1", "llm", kind=SpanKind.LLM,
              attrs={"http.status_code": 401, "retry.count": 5})
    a = build_hypotheses(_trace([s]))
    b = build_hypotheses(_trace([s]))
    assert [h.hypothesis_id for h in a] == [h.hypothesis_id for h in b]
    assert len(build_hypotheses(_trace([s]), max_hypotheses=1)) == 1


# -- failure_investigator specialist -------------------------------------------


def test_failure_investigator_wraps_hypotheses_capped() -> None:
    import json

    from forge_lens.ops.agents import run_specialist

    s = _span("a1", "llm", kind=SpanKind.LLM,
              attrs={"http.status_code": 401})
    run = run_specialist(
        "failure_investigator", _trace([s]),
        context={"max_hypotheses": 0},
    )
    f = run.findings[0]
    json.dumps(f)  # findings stay JSON-serializable
    assert f["category"] == "auth_error"
    assert f["hypotheses"] == []  # cap honored
    assert f["hypothesis_count"] == 1
    assert f["hypotheses_truncated"] is True
    assert f["eval_suggestions"]  # candidate eval specs ride along

    run2 = run_specialist("failure_investigator", _trace([s]))
    assert run2.findings[0]["hypotheses"]


# -- 12.7: evaluation suggestions ---------------------------------------------


def _report(category_trace: tuple[str, str]) -> FailureReport:
    cat, tid = category_trace
    return FailureReport(
        trace_id=tid,
        failed=True,
        category=FailureCategory(cat),
        evidence={"span_ids": ["1" * 16], "names": ["web_search"]},
    )


def test_suggest_evaluators_recurring_invalid_args() -> None:
    reports = [
        _report(("invalid_arguments", "a" * 32)),
        _report(("invalid_arguments", "b" * 32)),
    ]
    out = suggest_evaluators(reports)
    kinds = {(s["kind"], s["name"]) for s in out["suggestions"]}
    assert ("evaluator_spec", "allowed_tools") in kinds
    guard = next(s for s in out["suggestions"]
                 if s["name"] == "allowed_tools")
    assert guard["params"]["allowed"] == ["web_search"]
    ev = guard["evidence"]
    assert ev["recurring"] is True
    assert ev["count"] == 2
    assert all(s["classification"] == "candidate"
               for s in out["suggestions"])


def test_suggest_evaluators_timeout_and_budget() -> None:
    out = suggest_evaluators([_report(("timeout", "a" * 32))])
    lat = next(s for s in out["suggestions"]
               if s["name"] == "latency_budget")
    assert "max_duration_ms" in lat["params_required"]
    out2 = suggest_evaluators([_report(("budget_exceeded", "a" * 32))])
    assert any(s["name"] == "token_budget" for s in out2["suggestions"])


def test_suggest_evaluators_skips_unknown_and_healthy() -> None:
    out = suggest_evaluators([
        _report(("unknown", "a" * 32)),
        FailureReport(trace_id="b" * 32, failed=False),
    ])
    assert out["suggestions"] == []


def test_suggest_evaluators_merges_across_categories() -> None:
    """Two categories suggesting the same spec merge their evidence."""
    out = suggest_evaluators([
        _report(("auth_error", "a" * 32)),
        _report(("missing_permissions", "b" * 32)),
    ])
    ef = [s for s in out["suggestions"] if s["name"] == "error_free"]
    assert len(ef) == 1
    assert sorted(ef[0]["evidence"]["categories"]) == [
        "auth_error", "missing_permissions"
    ]
    assert ef[0]["evidence"]["recurring"] is True
