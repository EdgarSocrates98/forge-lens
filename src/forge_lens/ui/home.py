"""Bare-command home for Forge Lens."""

from __future__ import annotations

from forge_lens.ui.kit import UIContext
from forge_lens.ui.tui import run_tui


def run_home(*, ctx: UIContext | None = None) -> int:
    return run_tui(ctx=ctx)


__all__ = ["run_home"]
