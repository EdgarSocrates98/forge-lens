"""The three terminal surfaces stay explicit and TTY-safe."""

from __future__ import annotations

import pytest

from forge_lens.ui.kit import NonInteractive
from forge_lens.ui.tui import run_tui
from forge_lens.ui.wizard import run_wizard


def test_wizard_is_a_real_tty_gated_surface() -> None:
    with pytest.raises(NonInteractive, match="requires a TTY"):
        run_wizard()


def test_tui_is_a_real_tty_gated_surface() -> None:
    with pytest.raises(NonInteractive, match="requires a TTY"):
        run_tui()
