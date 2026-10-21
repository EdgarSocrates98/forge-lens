"""Failure investigation (Cycle 05 backend).

Deterministic classification of *why* a trace failed, an ordered replay
timeline over recorded evidence, and a single-call incident bundle.
No heuristics claim certainty they lack — every classification carries
its evidence spans and a confidence level.
"""

from forge_lens.investigate.bundle import incident_bundle
from forge_lens.investigate.classify import FailureReport, classify_failure
from forge_lens.investigate.replay import replay_timeline

__all__ = [
    "FailureReport",
    "classify_failure",
    "incident_bundle",
    "replay_timeline",
]
