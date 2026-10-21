"""Shared base for all Forge Lens contract models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LensModel(BaseModel):
    """Base model for every public contract.

    Unknown fields are preserved (never silently dropped) so that payloads
    produced by newer contract versions remain readable — this is the
    "preserve unknown fields" rule from the product principles.
    """

    model_config = ConfigDict(extra="allow", use_enum_values=False)
