"""Evidence provenance — the epistemic spine of Forge Lens.

Nothing in the platform may be promoted from a weaker provenance to a
stronger one silently. ``INFERRED`` never becomes ``OBSERVED``.
"""

from __future__ import annotations

from enum import StrEnum


class Provenance(StrEnum):
    """How a piece of information arrived at the platform."""

    OBSERVED = "observed"
    """Directly collected from a live instrumented execution."""

    IMPORTED = "imported"
    """Received from an external source (file import, foreign format)."""

    DERIVED = "derived"
    """Computed deterministically from observed/imported data."""

    INFERRED = "inferred"
    """A hypothesis produced by heuristics or models — never asserted as fact."""

    UNKNOWN = "unknown"
    """Evidence is insufficient to classify the information."""
