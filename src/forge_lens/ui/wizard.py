"""Canonical Forge Lens installation wizard surface.

The implementation lives in :mod:`forge_lens.install.guided` because it owns
the real receipt lifecycle.  This module is intentionally a thin UI boundary:
every Forge exposes ``ui.wizard.run_wizard`` without duplicating install
semantics or creating a second state machine.
"""

from __future__ import annotations

from forge_lens.install.guided import guided_install_available, run_guided_install


def run_wizard() -> int:
    """Run the governed install wizard; fail closed when no TTY is present."""

    return run_guided_install()


__all__ = ["guided_install_available", "run_wizard"]
