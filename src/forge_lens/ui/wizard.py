"""Guided installation wizard (Experience Program §1.2).

Drives the *real* install service — every choice maps to an actual
``install()`` call; the review step is the real dry-run plan, and the
apply step is the governed ``--yes`` path. Nothing here writes files
itself.

Flow: env check → scope → profile → hosts → review (dry-run plan) →
confirm → apply → doctor → finish. Any step can be cancelled (Esc/q)
without side effects; nothing is written before the explicit confirm.
"""

from __future__ import annotations

import importlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from forge_lens.ui import i18n
from forge_lens.ui.kit import (
    NonInteractive,
    UIContext,
    _c,
    confirm,
    dashboard,
    multi_select,
    select,
)

_ALL_HOSTS = ("claude", "devin", "codex", "copilot")
_PROFILES = ("recommended", "minimal", "full")  # service contract names
_PROFILE_LABELS = {
    "recommended": "Balanced (Recommended)",
    "minimal": "Economy",
    "full": "Full",
}
_GATEWAY_PROFILES = ("economy", "balanced", "deep")
_GATEWAY_LABELS = {
    "economy": "Economy (lowest context cost)",
    "balanced": "Balanced (recommended)",
    "deep": "Deep (maximum verification)",
}
_SCOPE_LABELS = {
    "project": "Current repository",
    "workspace": "Entire workspace",
    "user": "My user account",
}


def _has_vcs_root(start: Path) -> bool:
    """Return whether current directory belongs to a supported project root."""
    return any(
        (candidate / ".git").exists()
        or ((candidate / "forge.json").is_file() and (candidate / "pyproject.toml").is_file())
        for candidate in (start, *start.parents)
    )


def _scope_choices(ctx: UIContext) -> list[str]:
    """Hide repository-only choice when operator is in a fresh directory."""
    scopes = ["project", "workspace", "user"]
    if not _has_vcs_root(Path.cwd()):
        print(
            _c(
                ctx,
                "33",
                "  No repository detected; choose Entire workspace to use this directory "
                "or My user account.",
            )
        )
        scopes.remove("project")
    return scopes


def env_check_rows() -> list[tuple[str, str]]:
    """Real environment evidence: python, git, host binaries."""
    v = sys.version_info
    rows = [("Python", f"{v.major}.{v.minor}.{v.micro}")]
    rows.append(("Git", "OK" if shutil.which("git") else "missing"))
    for h in _ALL_HOSTS:
        rows.append((h.capitalize(), "Available" if shutil.which(h) else "not detected"))
    return rows


def _banner(ctx: UIContext, forge_name: str) -> None:
    print()
    print(_c(ctx, "1", f" {forge_name.upper()}"))
    print(_c(ctx, "2", " " + ("─" if ctx.unicode else "-") * max(10, len(forge_name) + 4)))
    print(f"  {i18n.t('env_detected', ctx.language)}:")


def run_wizard(
    *,
    forge_name: str,
    install_fn: Any,
    doctor_fn: Any | None = None,
    hosts_available: tuple[str, ...] = _ALL_HOSTS,
    ctx: UIContext | None = None,
    out_json: bool = False,
    family: bool = False,
) -> int:
    """Interactive install wizard. Returns exit code.

    ``install_fn(**kwargs)`` is the forge's real install service
    (``host``/``scope``/``profile``/``yes``/``dry_run`` kwargs).
    ``doctor_fn`` optionally verifies post-install.
    """
    ctx = ctx or UIContext.detect()
    if not ctx.interactive:
        raise NonInteractive("install wizard requires a TTY")

    _banner(ctx, forge_name)
    env_rows = env_check_rows()
    for label, val in env_rows:
        print(f"    {label:<14} {_c(ctx, '32' if val in ('OK', 'Available') else '2', val)}")
    print()
    if family:
        print(_c(ctx, "36", "  family mode: The Forge + registered specialist forges"))
        print(_c(ctx, "2", "  one real plan, one approval, each forge keeps its own installer"))
        print()

    # --- scope ---------------------------------------------------------------
    scopes = _scope_choices(ctx)
    idx = select(
        i18n.t("install_scope_q", ctx.language),
        [_SCOPE_LABELS[s] for s in scopes],
        ctx=ctx,
    )
    if idx is None:
        return 130
    scope = scopes[idx]

    # --- profile -------------------------------------------------------------
    idx = select(
        i18n.t("install_profile_q", ctx.language),
        [_PROFILE_LABELS[p] for p in _PROFILES],
        ctx=ctx,
    )
    if idx is None:
        return 130
    profile = _PROFILES[idx]

    # --- execution profile --------------------------------------------------
    idx = select(
        i18n.t("install_gateway_profile_q", ctx.language),
        [_GATEWAY_LABELS[p] for p in _GATEWAY_PROFILES],
        ctx=ctx,
    )
    if idx is None:
        return 130
    gateway_profile = _GATEWAY_PROFILES[idx]

    # --- optional components ---------------------------------------------------
    # The profile decides the default; the operator can narrow or extend it.
    # "graph-studio"/"tui" are runtime flags persisted to components.json —
    # skills/agents/mcp map to real asset kinds.
    _COMP_LABELS = {
        "skills": "Skills",
        "agents": "Agents",
        "mcp": "MCP",
        "tui": "TUI",
        "graph-studio": "Graph Studio",
    }
    _COMP_ORDER = ("skills", "agents", "mcp", "tui", "graph-studio")
    _COMP_DETAILS = {
        "skills": "host-discoverable skills and progressive capability guidance",
        "agents": "specialists, subagents and declared agent instructions",
        "mcp": "MCP configuration and governed server entry points",
        "tui": "the terminal command center runtime",
        "graph-studio": "federated graph views and local graph navigation",
    }
    _profile_defaults = {
        "minimal": {"mcp", "tui"},
        "recommended": {"skills", "agents", "mcp", "tui", "graph-studio"},
        "full": {"skills", "agents", "mcp", "tui", "graph-studio"},
    }
    comp_checked = _profile_defaults.get(profile, set())
    print(_c(ctx, "2", "  component choices (economy loads optional knowledge/evals on demand):"))
    for component in _COMP_ORDER:
        print(f"    {component:<13} {_COMP_DETAILS[component]}")
    comp_chosen = multi_select(
        i18n.t("install_components_q", ctx.language),
        [_COMP_LABELS[c] for c in _COMP_ORDER],
        checked={i for i, c in enumerate(_COMP_ORDER) if c in comp_checked},
        ctx=ctx,
    )
    if comp_chosen is None:
        return 130
    components = tuple(sorted(_COMP_ORDER[i] for i in comp_chosen))

    # --- hosts ---------------------------------------------------------------
    detected = {
        i
        for i, h in enumerate(hosts_available)
        if any(h in r[0].lower() and r[1] == "Available" for r in env_rows)
    }
    chosen = multi_select(
        i18n.t("install_hosts_q", ctx.language),
        list(hosts_available),
        checked=detected,
        ctx=ctx,
    )
    if chosen is None:
        return 130
    if not chosen:
        host_arg: str | None = None
    elif len(chosen) == len(hosts_available):
        host_arg = "all"
    else:
        host_arg = ",".join(hosts_available[i] for i in sorted(chosen))

    # --- review (real dry-run plan) ------------------------------------------
    # Contrato explícito: install_fn aceita `components` — wrappers **kw
    # encaminham sem inspeção de assinatura (GAP-002).
    kwargs: dict[str, Any] = {
        "scope": scope,
        "profile": profile,
        "gateway_profile": gateway_profile,
        "components": components,
    }
    try:
        # `none` é opt-out explícito — zero hosts escolhidos nunca vira
        # "all" (GAP-003); csv de subconjunto é contrato válido (GAP-003b).
        plan = install_fn(host=host_arg or "none", dry_run=True, **kwargs)
    except Exception as exc:
        print(_c(ctx, "31", f"plan failed: {exc}"))
        return 1

    planned = plan.get("planned") or plan.get("planned_files") or plan.get("writes") or []
    rows: list[tuple[str, str]] = [
        ("scope", scope),
        ("profile", profile),
        ("execution", gateway_profile),
        ("mode", "family" if family else "this forge"),
        ("components", ", ".join(components) or "none"),
        ("hosts", host_arg or "none"),
        ("managed writes", str(len(planned) if isinstance(planned, list) else planned)),
    ]
    # install-2.1 — the consolidated plan on the review screen: workspace
    # root, discovered members, registered forges and the closed plan
    # categories (new/updated/reused/preserved/conflict/unsupported).
    ws = plan.get("workspace") if isinstance(plan.get("workspace"), dict) else {}
    inst = plan.get("installation") if isinstance(plan.get("installation"), dict) else {}
    if plan.get("target_root"):
        rows.append(("workspace", str(plan["target_root"])))
    members = ws.get("members") or inst.get("members") or []
    if members:
        names = [str(m.get("path")) if isinstance(m, dict) else str(m) for m in members]
        rows.append(("members", f"{len(names)}: {', '.join(names[:6])}"))
    forge_rows = [f for f in (plan.get("forges") or []) if isinstance(f, dict)]
    if forge_rows:
        fid_list = ", ".join(str(f.get("forge_id")) for f in forge_rows)
        rows.append(("forges", f"{len(forge_rows)}: {fid_list}"))
    if inst:
        counts: dict[str, int] = {}
        for f in inst.get("forges") or []:
            cls = str((f or {}).get("classification") or "?")
            counts[cls] = counts.get(cls, 0) + 1
        if counts:
            rows.append(("plan", " · ".join(f"{k} {n}" for k, n in sorted(counts.items()))))
        n_conflicts = len(inst.get("conflicts") or [])
        if n_conflicts:
            rows.append(("conflicts", str(n_conflicts)))
    dashboard(
        f"{forge_name} — {i18n.t('review', ctx.language)}",
        [("Plan", rows)],
        ctx=ctx,
    )
    # The full consolidated doc — same formatter the CLI approve path
    # prints; deterministic and identical in non-TTY/JSON modes.
    from forge_lens.ui.kit import format_status_doc

    sys.stdout.write(format_status_doc(plan, ctx=ctx))

    if not confirm(i18n.t("confirm_q", ctx.language), default=False, ctx=ctx):
        return 130

    # --- apply (real governed install) ---------------------------------------
    receipt = install_fn(host=host_arg or "none", yes=True, dry_run=False, **kwargs)
    status = receipt.get("status", "unknown")
    if out_json:
        print(json.dumps(receipt, indent=2, ensure_ascii=False))
    color = "32" if status == "completed" else "31"
    print(f"\n  {i18n.t('status', ctx.language)}: {_c(ctx, color, status)}")

    # --- verify --------------------------------------------------------------
    if doctor_fn is not None and status == "completed":
        health = doctor_fn(scope=scope)
        raw_health = health.get("status") or health.get("health") or "unknown"
        hstatus = raw_health
        if isinstance(raw_health, dict):
            hstatus = raw_health.get("status", "unknown")
        color = "32" if hstatus in ("healthy", "PASS") else "33"
        print(f"  {i18n.t('health', ctx.language)}: {_c(ctx, color, str(hstatus))}")

    if status == "completed":
        print("  next: theforge specialists status · theforge doctor · theforge portal")

    return 0 if status == "completed" else 1


def run_setup_wizard(
    *,
    root: Any = None,
    ctx: UIContext | None = None,
    out_json: bool = False,
) -> int:
    """Family onboarding over the same real plan/approval/apply wizard."""
    try:
        service = importlib.import_module("theforge.install.service")
    except ModuleNotFoundError as exc:
        if exc.name == "theforge" or (exc.name or "").startswith("theforge."):
            print(
                "family onboarding unavailable: install The Forge separately; "
                "native Forge commands remain available"
            )
            return 2
        raise

    return run_wizard(
        forge_name="the-forge family",
        install_fn=lambda **kw: service.install_auto(root=root, **kw),
        doctor_fn=lambda **kw: service.doctor(root=root, **kw),
        ctx=ctx,
        out_json=out_json,
        family=True,
    )
