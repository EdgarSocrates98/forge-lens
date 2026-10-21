"""Pricing registry — model → USD-per-million-tokens.

The bundled table is a snapshot of public list prices; it is a
*pricing source*, not ground truth — every estimate carries
``pricing_source`` + ``assumptions`` so consumers see the premise.
Operators override via ``pricing_file`` (JSON).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PRICING_SOURCE_ID = "forge-lens-bundled-2026-01"


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1M tokens. ``None`` fields mean "not priced" — never
    invent a rate the source did not publish."""

    input_per_mtok: float
    output_per_mtok: float
    cached_input_per_mtok: float | None = None


# Public list prices (USD/MTok) at snapshot time. Estimates derived
# from this table always name PRICING_SOURCE_ID as their source.
DEFAULT_PRICES: dict[str, ModelPrice] = {
    # OpenAI
    "gpt-4o": ModelPrice(2.50, 10.00, cached_input_per_mtok=1.25),
    "gpt-4o-mini": ModelPrice(0.15, 0.60, cached_input_per_mtok=0.075),
    "gpt-4.1": ModelPrice(2.00, 8.00, cached_input_per_mtok=0.50),
    "gpt-4.1-mini": ModelPrice(0.40, 1.60, cached_input_per_mtok=0.10),
    "gpt-4.1-nano": ModelPrice(0.10, 0.40, cached_input_per_mtok=0.025),
    "o3": ModelPrice(2.00, 8.00, cached_input_per_mtok=0.50),
    "o4-mini": ModelPrice(1.10, 4.40, cached_input_per_mtok=0.275),
    # Anthropic
    "claude-opus-4": ModelPrice(15.00, 75.00, cached_input_per_mtok=1.50),
    "claude-sonnet-4": ModelPrice(3.00, 15.00, cached_input_per_mtok=0.30),
    "claude-3-7-sonnet": ModelPrice(3.00, 15.00, cached_input_per_mtok=0.30),
    "claude-3-5-sonnet": ModelPrice(3.00, 15.00, cached_input_per_mtok=0.30),
    "claude-3-5-haiku": ModelPrice(0.80, 4.00, cached_input_per_mtok=0.08),
    "claude-3-haiku": ModelPrice(0.25, 1.25, cached_input_per_mtok=0.03),
}


class PricingRegistry:
    """Exact + longest-prefix model lookup over a priced table."""

    def __init__(
        self,
        prices: dict[str, ModelPrice] | None = None,
        *,
        source: str = PRICING_SOURCE_ID,
    ) -> None:
        self._prices = prices if prices is not None else dict(DEFAULT_PRICES)
        self.source = source

    @classmethod
    def from_file(cls, path: Path) -> PricingRegistry:
        """JSON override: ``{"model": {"input_per_mtok": 1.0, ...}}``.

        Every failure names the file and the offending model — operator
        config errors should diagnose themselves.
        """
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise ValueError(f"pricing file {path} unreadable: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"pricing file {path} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"pricing file {path} must be a JSON object")
        prices: dict[str, ModelPrice] = {}
        for model, entry in raw.items():
            if not isinstance(entry, dict):
                raise ValueError(
                    f"pricing file {path}: entry for {model!r} must be an object"
                )
            try:
                prices[str(model)] = ModelPrice(
                    input_per_mtok=float(entry["input_per_mtok"]),
                    output_per_mtok=float(entry["output_per_mtok"]),
                    cached_input_per_mtok=(
                        float(v)
                        if (v := entry.get("cached_input_per_mtok")) is not None
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"pricing file {path}: invalid entry for {model!r}: {exc}"
                ) from exc
        return cls(prices, source=f"file:{path.name}")

    def lookup(self, model: str | None) -> tuple[str, ModelPrice] | None:
        """Resolve ``model`` to ``(registry_key, price)``.

        Exact match first, then longest registry key that is a
        dash-boundary prefix (``gpt-4o-2024-08-06`` → ``gpt-4o``).
        """
        if not model:
            return None
        if model in self._prices:
            return model, self._prices[model]
        candidates = [k for k in self._prices if model.startswith(f"{k}-")]
        if candidates:
            key = max(candidates, key=len)
            return key, self._prices[key]
        return None

    def estimate(
        self,
        *,
        model: str | None,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_input_tokens: int | None = None,
        reasoning_tokens: int | None = None,
    ) -> tuple[float | None, list[str]]:
        """USD estimate + explicit assumptions; ``None`` when unpriced.

        Cached input bills at the cached rate when one exists, and the
        uncached remainder at the input rate. Reasoning tokens bill as
        output (provider convention). ``None`` counters are treated as
        zero *in the estimate* and recorded as an assumption.
        """
        resolved = self.lookup(model)
        if resolved is None:
            return None, []
        key, price = resolved
        assumptions = [
            f"model {model!r} resolved to registry entry {key!r} ({self.source})"
        ]
        if input_tokens is None or output_tokens is None:
            assumptions.append("missing token counters treated as zero")
        cached = cached_input_tokens or 0
        uncached = max(0, (input_tokens or 0) - cached)
        cached_rate = (
            price.cached_input_per_mtok
            if price.cached_input_per_mtok is not None
            else price.input_per_mtok
        )
        if cached and price.cached_input_per_mtok is None:
            assumptions.append("no cached rate — cached tokens billed at input rate")
        if reasoning_tokens:
            assumptions.append("reasoning tokens billed as output")
        usd = (
            uncached * price.input_per_mtok
            + cached * cached_rate
            + ((output_tokens or 0) + (reasoning_tokens or 0))
            * price.output_per_mtok
        ) / 1_000_000.0
        return usd, assumptions

    def models(self) -> dict[str, Any]:
        """Serializable view of the table (for diagnostics/API)."""
        return {
            k: {
                "input_per_mtok": v.input_per_mtok,
                "output_per_mtok": v.output_per_mtok,
                "cached_input_per_mtok": v.cached_input_per_mtok,
            }
            for k, v in sorted(self._prices.items())
        }
