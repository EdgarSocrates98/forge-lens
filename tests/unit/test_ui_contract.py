"""The three terminal surfaces stay explicit and TTY-safe."""

from __future__ import annotations

import pytest

from forge_lens.install.guided import run_guided_install
from forge_lens.ui.kit import NonInteractive
from forge_lens.ui.tui import run_tui


def test_wizard_is_a_real_tty_gated_surface() -> None:
    with pytest.raises(NonInteractive, match="requires a TTY"):
        run_guided_install()


def test_tui_is_a_real_tty_gated_surface() -> None:
    with pytest.raises(NonInteractive, match="requires a TTY"):
        run_tui()
