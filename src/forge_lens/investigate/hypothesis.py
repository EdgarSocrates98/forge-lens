"""Root-cause hypotheses (Cycle 05 §05.4).

A hypothesis is an *explainable candidate cause*: every instance links
the signals that support it, the signals that point elsewhere, the
evidence that would confirm or deny it but was never captured, and
concrete verification steps. Status stays honest — ``proposed`` when
evidence is thin, ``supported`` on multiple signals, ``refuted`` only
when contradictory evidence exists, ``unverifiable`` when the captured
data cannot decide.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.v1.trace import Span, Trace
from forge_lens.investigate.classify import (
    _PRIORITY,
    Confidence,
    FailureCategory,
    FailureReport,
    Signal,
    _Scan,
    _scan_trace,
    classify_failure,
)

MAX_HYPOTHESES = 10
"""Default bound — one per plausible category is already a ceiling; the
cap exists so a pathological trace cannot emit an unbounded list."""


class HypothesisStatus(StrEnum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNVERIFIABLE = "unverifiable"


class Hypothesis(LensModel):
    """A candidate root cause with its full evidence ledger.

    ``label`` is the failure category this hypothesis argues for;
    ``hypothesis_id`` is deterministic (``trace_id:category``) so the
    same trace always yields the same ids.
    """

    hypothesis_id: str
    label: str
    symptom: str
    supporting_evidence: list[str] = Field(default_factory=list)
    contradictory_evidence: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    suggested_verification: list[str] = Field(default_factory=list)
    confidence: Confidence = "observed"
    status: HypothesisStatus = HypothesisStatus.PROPOSED


# Evidence that would *confirm* each cause but is often absent from the
# capture — naming the gap is more honest than implying certainty.
_MISSING: dict[FailureCategory, tuple[str, ...]] = {
    FailureCategory.AUTH_ERROR: (
        "credential identity (which key/token was presented) not recorded",
        "no www-authenticate / auth response header captured",
    ),
    FailureCategory.MISSING_PERMISSIONS: (
        "required vs granted scopes/roles not recorded",
        "provider 403 payload not captured — set capture=full to read it",
    ),
    FailureCategory.INVALID_ARGUMENTS: (
        "rejected arguments not captured — set capture=full to diff them "
        "against the tool input schema",
        "declared tool input schema not recorded",
    ),
    FailureCategory.SCHEMA_MISMATCH: (
        "response body not captured — set capture=full to verify "
        "schema_mismatch against the declared output schema",
        "declared output schema/response_format not recorded",
    ),
    FailureCategory.RETRY_EXHAUSTION: (
        "configured retry maximum absent (retry.max attr) — threshold "
        "assumed, not verified",
        "retry policy config (backoff/jitter) not recorded",
    ),
    FailureCategory.AGENT_CANCELLED: (
        "cancellation source not recorded (user action, parent task, "
        "or an external deadline)",
    ),
    FailureCategory.BUDGET_EXCEEDED: (
        "configured budget not recorded (budget.limit / provider quota) "
        "— observed usage cannot be compared to a limit",
    ),
    FailureCategory.RATE_LIMIT: (
        "retry-after / quota headers not captured — throttle window and "
        "scope unknown",
    ),
    FailureCategory.TIMEOUT: (
        "configured deadline not recorded — span duration cannot be "
        "compared to the limit that fired",
    ),
    FailureCategory.TOOL_ERROR: (
        "tool input/output not captured — set capture=full to replay "
        "the failing call",
    ),
    FailureCategory.LLM_ERROR: (
        "provider error payload not captured — model/region outage vs "
        "request defect undecidable",
    ),
    FailureCategory.GUARDRAIL: (
        "guardrail rule/verdict detail not recorded — which rule fired "
        "is unverifiable",
    ),
    FailureCategory.CYCLE: (
        "producer-side span context not captured — which parent pointer "
        "was corrupted upstream is unverifiable",
    ),
    FailureCategory.INCOMPLETE: (
        "no host-process telemetry (exit code, OOM, heartbeat) — crash "
        "vs hang undetermined",
    ),
    FailureCategory.UNKNOWN: (
        "no recognizable failure signature captured — widen capture "
        "(exception events, http status codes, status messages) and "
        "re-classify",
    ),
}

_VERIFICATION: dict[FailureCategory, tuple[str, ...]] = {
    FailureCategory.AUTH_ERROR: (
        "re-run with a known-good credential; check expiry/rotation on "
        "the failing key",
        "verify the provider endpoint + auth scheme expected by the call",
    ),
    FailureCategory.MISSING_PERMISSIONS: (
        "diff required scopes/ACLs of the call against the granted set "
        "on the service account",
        "inspect the provider 403 response body for the missing scope",
    ),
    FailureCategory.INVALID_ARGUMENTS: (
        "diff the recorded tool args against the tool input schema; "
        "re-run the tool with corrected args in isolation",
    ),
    FailureCategory.SCHEMA_MISMATCH: (
        "capture the full output (capture=full) and validate it against "
        "the declared schema; check for model/version drift",
    ),
    FailureCategory.RETRY_EXHAUSTION: (
        "inspect the retry policy config (max attempts, backoff) and "
        "check whether the underlying error is permanent",
    ),
    FailureCategory.AGENT_CANCELLED: (
        "locate the cancellation source — user action, parent-task "
        "cancellation, or an enclosing deadline",
    ),
    FailureCategory.BUDGET_EXCEEDED: (
        "compare observed token/cost usage against the configured "
        "budget; raise the limit or shrink context",
    ),
    FailureCategory.RATE_LIMIT: (
        "check provider quota/rpm and the retry-after window; confirm "
        "backoff is applied",
    ),
    FailureCategory.TIMEOUT: (
        "compare span duration_ms against the configured deadline; "
        "check downstream latency at the same wall-clock window",
    ),
    FailureCategory.TOOL_ERROR: (
        "re-run the tool with the captured args outside the agent to "
        "isolate tool vs orchestration",
    ),
    FailureCategory.LLM_ERROR: (
        "check provider status for the failing model+region; retry the "
        "same request payload",
    ),
    FailureCategory.GUARDRAIL: (
        "inspect which guardrail rule fired and its recorded verdict",
    ),
    FailureCategory.CYCLE: (
        "audit the producer's parent propagation — the stored parent "
        "pointers form an impossible loop",
    ),
    FailureCategory.INCOMPLETE: (
        "check host logs around ended_at for crash/OOM/kill evidence",
    ),
    FailureCategory.UNKNOWN: (
        "widen capture (events, exception types, http status codes) "
        "and re-classify",
    ),
}

_CONTRADICTION_CAP = 10


def _support(hits: list[tuple[Span, list[Signal]]]) -> list[str]:
    return sorted(
        f"{s.span_id}: {label}"
        for s, sigs in hits
        for label, _strong in sigs
    )


def _contradictions(
    cat: FailureCategory,
    hits: list[tuple[Span, list[Signal]]],
    scan: _Scan,
) -> tuple[list[str], bool]:
    """Signals pointing elsewhere — and whether *every* supporting span
    is out-competed by a strong signal for another category on the same
    span (the only honest path to ``refuted``)."""
    out: list[str] = []
    all_outcompeted = True
    for s, _sigs in hits:
        rivals: list[str] = []
        for other, other_sigs in scan.signals[s.span_id].items():
            if other is cat:
                continue
            rivals.extend(
                label for label, strong in other_sigs if strong
            )
        for label in sorted(rivals):
            out.append(f"{s.span_id}: {label} — argues against {cat.value}")
        if not rivals:
            all_outcompeted = False
    # strong competitor signals on *other* spans count as contradictory
    # context too (the scan reads the whole trace)
    own_spans = {s.span_id for s, _ in hits}
    for other, other_hits in scan.matched.items():
        if other is cat:
            continue
        for os_, osigs in other_hits:
            if os_.span_id in own_spans:
                continue
            for label, strong in osigs:
                if strong:
                    out.append(
                        f"{os_.span_id}: {label} — suggests "
                        f"{other.value} instead"
                    )
    return sorted(set(out))[:_CONTRADICTION_CAP], all_outcompeted


def _missing(cat: FailureCategory, hits: list[tuple[Span, list[Signal]]]) -> list[str]:
    out = list(_MISSING.get(cat, ()))
    all_weak = all(
        not strong for _s, sigs in hits for _label, strong in sigs
    )
    if all_weak:
        out.append(
            "only free-text mention — no status code, exception type, "
            "counter or flag recorded"
        )
    no_types = all(
        not any(
            label.startswith("exception_type:")
            for label, _ in sigs
        )
        for _s, sigs in hits
    )
    if no_types:
        out.append(
            "no exception.type recorded on the failing span(s) — capture "
            "exception events to pin the signature"
        )
    return out


def _signal_hypothesis(
    trace: Trace,
    cat: FailureCategory,
    hits: list[tuple[Span, list[Signal]]],
    scan: _Scan,
    primary: bool,
) -> Hypothesis:
    supporting = _support(hits)
    contradictory, all_outcompeted = _contradictions(cat, hits, scan)
    strong = any(strong for _s, sigs in hits for _l, strong in sigs)
    n_signals = sum(len(sigs) for _s, sigs in hits)
    names = sorted({s.name for s, _ in hits})
    status = HypothesisStatus.PROPOSED
    if not strong and all_outcompeted and contradictory:
        status = HypothesisStatus.REFUTED
    elif n_signals >= 2 or len(hits) >= 2:
        status = HypothesisStatus.SUPPORTED
    return Hypothesis(
        hypothesis_id=f"{trace.trace_id}:{cat.value}",
        label=cat.value,
        symptom=(
            f"{'primary' if primary else 'competing'} cause candidate "
            f"{cat.value} — {len(hits)} span(s) carry {cat.value} signals "
            f"({', '.join(names)})"
        ),
        supporting_evidence=supporting,
        contradictory_evidence=contradictory,
        missing_evidence=_missing(cat, hits),
        suggested_verification=list(_VERIFICATION.get(cat, ())),
        confidence="observed" if strong else "inferred",
        status=status,
    )


def _structural_hypothesis(
    trace: Trace, report: FailureReport
) -> Hypothesis:
    """Hypothesis for categories without signal scans — cycle, kind
    buckets, incomplete. Evidence comes from the report itself."""
    cat = report.category
    span_ids = list(report.evidence.get("span_ids") or [])
    supporting = [f"span {sid}: {report.detail}" for sid in span_ids]
    if not supporting and report.detail:
        supporting = [report.detail]
    missing = list(_MISSING.get(cat, ()))
    if cat is FailureCategory.INCOMPLETE:
        missing.append(
            "host exit telemetry absent — structural inference, not a "
            "recorded error signature"
        )
    return Hypothesis(
        hypothesis_id=f"{trace.trace_id}:{cat.value}",
        label=cat.value,
        symptom=f"primary cause candidate {cat.value} — {report.detail}",
        supporting_evidence=supporting,
        contradictory_evidence=[],
        missing_evidence=missing,
        suggested_verification=list(_VERIFICATION.get(cat, ())),
        confidence="inferred" if report.confidence == "inferred" else "observed",
        status=(
            HypothesisStatus.SUPPORTED
            if len(supporting) >= 2
            else HypothesisStatus.PROPOSED
        ),
    )


def build_hypotheses(
    trace: Trace,
    report: FailureReport | None = None,
    *,
    max_hypotheses: int = MAX_HYPOTHESES,
) -> list[Hypothesis]:
    """Candidate root causes for a failed trace, evidence-ledgers intact.

    Every signal-matched category becomes a hypothesis — the report's
    primary category plus its competitors — so concurrent explanations
    stay visible instead of collapsing into a single verdict. A healthy
    trace yields no hypotheses; an unclassifiable failure yields one
    honest ``unverifiable`` hypothesis naming the capture gaps.
    """
    report = report or classify_failure(trace)
    if not report.failed:
        return []
    scan = _scan_trace(trace)

    out: list[Hypothesis] = []
    emitted: set[FailureCategory] = set()
    for cat in _PRIORITY:
        hits = list(scan.matched.get(cat, []))
        if cat is FailureCategory.AGENT_CANCELLED:
            hits.extend(scan.cancelled)
        if not hits:
            continue
        out.append(
            _signal_hypothesis(trace, cat, hits, scan, report.category is cat)
        )
        emitted.add(cat)

    # structural / kind-bucket primary that signals did not cover
    if report.category not in emitted and report.category not in (
        FailureCategory.UNKNOWN,
    ):
        out.append(_structural_hypothesis(trace, report))

    if not out:
        out.append(
            Hypothesis(
                hypothesis_id=f"{trace.trace_id}:unknown",
                label=FailureCategory.UNKNOWN.value,
                symptom=(
                    "trace failed but spans carry no recognizable "
                    "failure signature"
                ),
                supporting_evidence=[
                    f"span {sid}: ended in error without a known signature"
                    for sid in sorted(
                        s.span_id for s in scan.errs
                    )
                ],
                missing_evidence=list(_MISSING[FailureCategory.UNKNOWN]),
                suggested_verification=list(
                    _VERIFICATION[FailureCategory.UNKNOWN]
                ),
                confidence="inferred",
                status=HypothesisStatus.UNVERIFIABLE,
            )
        )

    # deterministic order: primary first, then remaining categories in
    # priority order, ties by hypothesis_id
    def _rank(h: Hypothesis) -> tuple[int, str]:
        if h.label == report.category.value:
            return (0, h.hypothesis_id)
        try:
            idx = _PRIORITY.index(FailureCategory(h.label))
        except ValueError:
            idx = len(_PRIORITY)
        return (1 + idx, h.hypothesis_id)

    out.sort(key=_rank)
    return out[: max(0, max_hypotheses)]


__all__ = [
    "MAX_HYPOTHESES",
    "Hypothesis",
    "HypothesisStatus",
    "build_hypotheses",
]
