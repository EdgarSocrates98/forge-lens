"""Guided `forge-lens install` — TTY wizard over the receipt lifecycle.

Same UX contract as `sparkforge-aws install` / `productforge install`:
env check → choices → real dry-run plan → confirm → apply → doctor.
Any step cancels (Esc/q) without side effects; nothing is written
before the explicit confirm. Lens-honest difference: `install` records
a receipt (the portable bootstrap performs the real install), so the
wizard asks the receipt's own fields — data dir, profile, source,
managed files — not host/gateway/component vocabularies it lacks.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from forge_lens.install import lifecycle
from forge_lens.ui import i18n
from forge_lens.ui.kit import (
    NonInteractive,
    UIContext,
    _c,
    confirm,
    dashboard,
    prompt,
    select,
)

_PROFILE_LABELS = {
    "recommended": "Balanced (Recommended)",
    "minimal": "Economy",
    "full": "Full",
}
_SOURCES = ("pypi", "wheel", "git", "zip", "local", "gitless", "offline")


def _tty() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _env_rows() -> list[tuple[str, str]]:
    v = sys.version_info
    return [
        ("Python", f"{v.major}.{v.minor}.{v.micro}"),
        ("Git", "OK" if shutil.which("git") else "missing"),
    ]


def run_guided_install() -> int:
    """Interactive wizard — returns an exit code; needs a real TTY."""
    ctx = UIContext.detect()
    if not ctx.interactive:
        raise NonInteractive("install wizard requires a TTY")

    print()
    print(_c(ctx, "1", " FORGE-LENS"))
    print(_c(ctx, "2", " " + ("─" if ctx.unicode else "-") * 14))
    print(f"  {i18n.t('env_detected', ctx.language)}:")
    for label, val in _env_rows():
        print(f"    {label:<14} {_c(ctx, '32' if val == 'OK' else '2', val)}")
    print()

    # --- data dir -------------------------------------------------------------
    data_dir = prompt("Data directory", default="./.forge-lens", ctx=ctx)
    if data_dir is None:
        return 130

    # --- profile ---------------------------------------------------------------
    profiles = ("recommended", "minimal", "full")
    idx = select(
        i18n.t("install_profile_q", ctx.language),
        [_PROFILE_LABELS[p] for p in profiles],
        ctx=ctx,
    )
    if idx is None:
        return 130
    profile = profiles[idx]

    # --- source ----------------------------------------------------------------
    idx = select(
        "Install source?",
        list(_SOURCES),
        ctx=ctx,
    )
    if idx is None:
        return 130
    source = _SOURCES[idx]

    # --- managed files ----------------------------------------------------------
    raw_files = prompt(
        "Managed files (csv, relative to data dir; empty = receipt only)",
        ctx=ctx,
    )
    if raw_files is None:
        return 130
    files = [f.strip() for f in raw_files.split(",") if f.strip()]

    target = Path(data_dir)

    # --- review (real dry-run plan) --------------------------------------------
    plan = lifecycle.install(
        target,
        profile=profile,
        source=source,
        files=files,
        ts_ns=0,
        dry_run=True,
    )
    if not plan.get("ok"):
        print(_c(ctx, "31", f"plan failed: {plan.get('error', '?')}"))
        return 1
    dashboard(
        f"forge-lens — {i18n.t('review', ctx.language)}",
        [
            (
                "Plan",
                [
                    ("data dir", str(target)),
                    ("profile", profile),
                    ("source", source),
                    ("managed files", str(len(files))),
                    ("receipt", str(target / "install" / "receipt.json")),
                ],
            )
        ],
        ctx=ctx,
    )
    for f in files[:10]:
        print(f"    {_c(ctx, '2', f)}")
    if len(files) > 10:
        print(f"    … {len(files) - 10} more")

    if not confirm(i18n.t("confirm_q", ctx.language), default=True, ctx=ctx):
        return 130

    # --- apply (real governed install) ------------------------------------------
    import time

    out = lifecycle.install(
        target,
        profile=profile,
        source=source,
        files=files,
        ts_ns=time.time_ns(),
        dry_run=False,
    )
    ok = bool(out.get("ok"))
    color = "32" if ok else "31"
    status = "completed" if ok else "failed"
    print(f"\n  {i18n.t('status', ctx.language)}: {_c(ctx, color, status)}")
    if not ok:
        print(f"  {_c(ctx, '31', str(out.get('error', 'install failed')))}")
        return 1
    print(f"  receipt: {_c(ctx, '2', str(target / 'install' / 'receipt.json'))}")

    # --- verify ------------------------------------------------------------------
    doc = lifecycle.doctor(target)
    hok = bool(doc.get("ok"))
    print(
        f"  {i18n.t('health', ctx.language)}: "
        + _c(ctx, "32" if hok else "33", "healthy" if hok else "degraded")
    )
    for c in doc.get("checks", []):
        mark = (
            _c(ctx, "32", "✓" if ctx.unicode else "v")
            if c.get("ok")
            else _c(ctx, "31", "✗" if ctx.unicode else "x")
        )
        print(f"    {mark} {c.get('check', '?')}")

    print("  next: forge-lens install doctor · forge-lens install-status")
    return 0


def guided_install_available() -> bool:
    """Bare `install` may route to the wizard only on a real TTY."""
    return _tty()


__all__ = ["guided_install_available", "run_guided_install"]
