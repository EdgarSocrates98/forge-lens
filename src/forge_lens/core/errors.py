"""Typed errors used across the platform."""

from __future__ import annotations


class LensError(Exception):
    """Base class for all Forge Lens errors."""


class ValidationError(LensError):
    """Payload failed contract validation."""


class PayloadTooLargeError(LensError):
    """Request exceeds configured payload limits."""

    def __init__(self, detail: str, limit: int) -> None:
        super().__init__(detail)
        self.limit = limit


class RateLimitedError(LensError):
    """Request rejected by the ingestion rate limiter."""


class StoreError(LensError):
    """Persistence-layer failure."""


class AuthError(LensError):
    """Authentication/authorization failure."""
