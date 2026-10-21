"""Waste + anomaly heuristics over a stored trace.

Every finding names its evidence (span ids + the counters observed) —
no finding without something a reviewer can open and verify.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.v1.trace import IntegrityFlag, SpanKind, Trace
from forge_lens.intel.cost import extract_usage
from forge_lens.intel.pricing import PricingRegistry


class WasteFinding(LensModel):
    """One evidence-backed waste/anomaly observation."""

    kind: str
    span_ids: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    estimated_usd: float | None = None
    """USD attached only when the finding is token-priced."""
    detail: str = ""


def _arg_fingerprint(span_attrs: dict[str, Any]) -> str | None:
    """Stable hash of tool-call arguments — identical retries share it."""
    for key in ("forge_lens.tool_args", "tool.parameters", "tool_call.arguments"):
        raw = span_attrs.get(key)
        if isinstance(raw, (str, dict)):
            blob = raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True)
            return hashlib.sha256(blob.encode()).hexdigest()[:16]
    return None


def detect_waste(trace: Trace, registry: PricingRegistry) -> list[WasteFinding]:
    """Run every heuristic over one trace; findings are additive.

    Deterministic: spans processed in ``(start_time, span_id)`` order.
    """
    findings: list[WasteFinding] = []
    spans = sorted(trace.spans, key=lambda s: (s.start_time, s.span_id))

    # -- retry amplification: same name + same args ≥3x under one parent
    retry_groups: dict[tuple[str | None, str, str], list[str]] = {}
    for s in spans:
        if s.kind not in (SpanKind.TOOL, SpanKind.LLM):
            continue
        fp = _arg_fingerprint(s.attributes)
        if fp is None:
            continue
        key = (s.parent_span_id, s.name, fp)
        retry_groups.setdefault(key, []).append(s.span_id)
    for (parent, name, _fp), ids in retry_groups.items():
        if len(ids) >= 3:
            findings.append(
                WasteFinding(
                    kind="retry_amplification",
                    span_ids=ids,
                    evidence={
                        "name": name,
                        "parent_span_id": parent,
                        "invocations": len(ids),
                    },
                    detail=(
                        f"{name!r} invoked {len(ids)}x with identical arguments — "
                        "possible retry loop"
                    ),
                )
            )

    # -- context replay: total input tokens vs largest single context
    llm_usages = [
        (s.span_id, u)
        for s in spans
        if s.kind is SpanKind.LLM and (u := extract_usage(s)) is not None
    ]
    inputs = [u.input_tokens or 0 for _, u in llm_usages if u.input_tokens]
    if len(inputs) >= 2 and max(inputs) > 0:
        # replayed = tokens re-sent beyond the largest single context —
        # flag when it reaches that largest window (context fully re-sent
        # at least once is the meaningful threshold)
        replayed = sum(inputs) - max(inputs)
        if replayed >= max(inputs):
            findings.append(
                WasteFinding(
                    kind="context_replay",
                    span_ids=[sid for sid, _ in llm_usages],
                    evidence={
                        "llm_calls": len(inputs),
                        "sum_input_tokens": sum(inputs),
                        "max_single_context": max(inputs),
                    },
                    detail=(
                        f"LLM chain re-sent ~{replayed} tokens of prior context — "
                        "consider caching/trimming"
                    ),
                )
            )

    # -- priced retry waste: tokens spent on spans flagged duplicate/conflict
    flagged = [
        s
        for s in spans
        if {f.value for f in s.integrity_flags}
        & {IntegrityFlag.DUPLICATE.value, IntegrityFlag.CONFLICT.value}
    ]
    wasted_usd = 0.0
    wasted_ids = []
    for s in flagged:
        if (u := extract_usage(s)) is None:
            continue
        usd, _ = registry.estimate(
            model=u.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cached_input_tokens=u.cached_input_tokens,
            reasoning_tokens=u.reasoning_tokens,
        )
        if usd is not None:
            wasted_usd += usd
        wasted_ids.append(s.span_id)
    if wasted_ids:
        findings.append(
            WasteFinding(
                kind="duplicate_delivery",
                span_ids=wasted_ids,
                evidence={"flagged_spans": len(flagged)},
                estimated_usd=round(wasted_usd, 8),
                detail=(
                    "spans re-delivered with duplicate/conflicting content — "
                    "token spend on work the store already had"
                ),
            )
        )

    # -- coverage gaps: llm spans without usage; unpriced models
    no_usage = [
        s.span_id
        for s in spans
        if s.kind is SpanKind.LLM and extract_usage(s) is None
    ]
    if no_usage:
        findings.append(
            WasteFinding(
                kind="unknown_tokens",
                span_ids=no_usage,
                evidence={"llm_spans_without_usage": len(no_usage)},
                detail="LLM spans carry no token_usage — cost is undercounted",
            )
        )
    unpriced = sorted(
        {
            u.model
            for s in spans
            if (u := extract_usage(s)) is not None
            and u.model
            and registry.lookup(u.model) is None
        }
    )
    if unpriced:
        findings.append(
            WasteFinding(
                kind="unpriced_model",
                evidence={"models": unpriced},
                detail="usage observed for models absent from the pricing registry",
            )
        )
    return findings
