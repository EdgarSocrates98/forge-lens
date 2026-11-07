"""Bare-command home for Forge Lens."""

from __future__ import annotations

from forge_lens.ui.kit import NonInteractive, UIContext
from forge_lens.ui.tui import run_tui


def run_home(*, ctx: UIContext | None = None) -> int:
    if ctx is not None and not ctx.interactive:
        raise NonInteractive("Forge Lens home requires a TTY")
    return run_tui(ctx=ctx)


__all__ = ["run_home"]
