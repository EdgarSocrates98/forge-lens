"""Blob references — pointers to payloads that live outside the contract."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from forge_lens.contracts.base import LensModel


class BlobKind(StrEnum):
    """Where a referenced payload physically lives."""

    INLINE = "inline"
    """Small payload embedded in the reference itself."""

    URI = "uri"
    """Externally resolvable location (file://, s3://, artifact store)."""

    SHA256 = "sha256"
    """Content-addressed payload; ``value`` is the hex digest."""

    ARTIFACT = "artifact"
    """Payload in the platform artifact store; ``value`` is the artifact id."""

    DROPPED = "dropped"
    """Payload intentionally not captured (privacy/size policy)."""


class BlobRef(LensModel):
    """A reference to a payload.

    The platform prefers references over inline content so prompts, tool
    arguments and outputs are captured only under an explicit capture level.
    """

    kind: BlobKind
    value: str
    """Payload content or locator, depending on ``kind``."""

    media_type: str | None = None
    size_bytes: int | None = None
    sha256: str | None = None
    """Integrity digest of the referenced payload, when known."""

    metadata: dict[str, Any] = Field(default_factory=dict)
