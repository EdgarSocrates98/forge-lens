"""Forge UI Kit — stdlib-only interactive terminal layer (vendored from
spark-forge-aws; byte-adapted for forge_lens imports).

Zero dependencies, cross-platform (msvcrt on Windows, termios on
POSIX). Every interaction has a headless equivalent so automation never
blocks on a TTY that isn't there.
"""

from forge_lens.ui.i18n import t
from forge_lens.ui.kit import (
    UIContext,
    confirm,
    dashboard,
    multi_select,
    prompt,
    select,
    status_table,
)
from forge_lens.ui.home import run_home
from forge_lens.ui.tui import run_tui

__all__ = [
    "UIContext",
    "confirm",
    "dashboard",
    "multi_select",
    "prompt",
    "select",
    "status_table",
    "run_home",
    "run_tui",
    "t",
]
