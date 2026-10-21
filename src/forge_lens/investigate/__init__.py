"""Failure investigation (Cycle 05 backend).

Deterministic classification of *why* a trace failed (11-category §05.3
taxonomy over recorded span evidence), root-cause hypotheses with
evidence ledgers (§05.4), eval-scenario suggestions from recurring
failures (§12.7), an ordered replay timeline, and a single-call
incident bundle. No heuristics claim certainty they lack — every
classification carries its evidence spans and a confidence level.
"""

from forge_lens.investigate.bundle import incident_bundle
from forge_lens.investigate.classify import (
    FailureCategory,
    FailureReport,
    classify_failure,
)
from forge_lens.investigate.eval_suggest import suggest_evaluators
from forge_lens.investigate.hypothesis import (
    Hypothesis,
    HypothesisStatus,
    build_hypotheses,
)
from forge_lens.investigate.replay import replay_timeline

__all__ = [
    "FailureCategory",
    "FailureReport",
    "Hypothesis",
    "HypothesisStatus",
    "build_hypotheses",
    "classify_failure",
    "incident_bundle",
    "replay_timeline",
    "suggest_evaluators",
]
