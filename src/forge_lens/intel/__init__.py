"""Token/cost/context intelligence (Cycle 04).

Derives cost estimates from observed ``forge_lens.token_usage``
attributes via a versioned pricing registry, and flags waste patterns.
Everything here is DERIVED provenance — estimates are never confused
with provider-billed cost.
"""

from forge_lens.intel.cost import CostReport, SpanCost, trace_cost
from forge_lens.intel.pricing import ModelPrice, PricingRegistry
from forge_lens.intel.waste import WasteFinding, detect_waste

__all__ = [
    "CostReport",
    "ModelPrice",
    "PricingRegistry",
    "SpanCost",
    "WasteFinding",
    "detect_waste",
    "trace_cost",
]
