"""Deterministic-core boundary — AST proofs for the pure packages.

``core/``, ``contracts/``, ``otlp/``, ``sanitize/``, ``storage/`` and
``ingest/`` carry the evidence spine (AGENTS.md hard rules): no network
imports, no subprocess, no civil-time reads. All I/O lives on the
``api/``/``cli``/``sdk`` boundary; these tests prove the line by walking
the AST of every module — a convention doc alone is not a boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "forge_lens"
PURE_DIRS = ("core", "contracts", "otlp", "sanitize", "storage", "ingest")

# Import roots banned across the pure tree — a pure module that opens
# sockets, speaks HTTP, or spawns processes is a boundary violation.
# ``asyncio`` is banned whole: the pure tree is synchronous, so any
# asyncio import there exists only to reach network/event-loop helpers.
_FORBIDDEN_IMPORT_ROOTS = {
    "aiohttp",
    "asyncio",
    "http",
    "httpx",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}

# Civil-time reads banned inside the pure tree — the deterministic core
# never consults a wall clock. Durations come from *injectable* clocks
# and civil timestamps arrive as data or via ``api/clock.py``, the
# boundary seam outside this tree.
_FORBIDDEN_ATTR_CALLS = {
    "time": {"time", "time_ns"},
    "datetime": {"now", "utcnow", "today"},
    "date": {"today"},
}
# These convert a clock argument when given one — only the zero-arg
# call reads the wall clock, so only zero-arg calls are banned.
_FORBIDDEN_ZERO_ARG = {
    "time": {"localtime", "gmtime", "ctime", "asctime", "strftime"},
}
# Fully-qualified targets a bare call can resolve to
# (``from time import time`` → ``time()``).
_FORBIDDEN_BARE = {
    "time.time",
    "time.time_ns",
    "time.tzset",
    "datetime.now",
    "datetime.utcnow",
    "datetime.today",
    "date.today",
}
_CLOCK_MODULES = set(_FORBIDDEN_ATTR_CALLS) | set(_FORBIDDEN_ZERO_ARG)

# Documented clock exceptions (AGENTS.md) — ``time.monotonic`` /
# ``perf_counter`` measure *durations* and are injectable defaults,
# never evidence. The set below pins exactly which pure files may use
# them; adding a user requires a recorded reason here, not a silent
# carve-out.
_ALLOWED_CLOCK_FILES = {
    "core/metrics.py",  # self-observability durations (perf_counter)
    "ingest/pipeline.py",  # injectable default clock (monotonic)
}
_MEASURED_CALLS = {"monotonic", "perf_counter", "process_time"}

# Whole-file exemptions — currently empty by design: the allowed
# surfaces above are call-scoped, so no file is exempt from the scan.
_ALLOWLIST: dict[str, str] = {}


def _pure_files() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for d in PURE_DIRS:
        for path in sorted((SRC / d).rglob("*.py")):
            out.append((path.relative_to(SRC).as_posix(), path))
    return out


def _bindings(tree: ast.AST) -> dict[str, str]:
    """Local name → canonical target for clock modules only.

    ``import time as t`` → ``t: time``; ``from datetime import datetime``
    → ``datetime: datetime``; ``from time import time`` →
    ``time: time.time``.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _CLOCK_MODULES and alias.name == root:
                    bound[alias.asname or alias.name] = root
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod not in _CLOCK_MODULES:
                continue
            for alias in node.names:
                bound[alias.asname or alias.name] = f"{mod}.{alias.name}"
    return bound


def _clock_scan(rel: str, tree: ast.AST) -> tuple[list[str], set[str]]:
    """(banned call sites, files using an allowed duration clock).

    Duration clocks are tracked on any ``time.<fn>`` *reference* — an
    injectable ``clock = time.monotonic`` default is usage even though
    nothing calls it in this module.
    """
    bound = _bindings(tree)
    out: list[str] = []
    measured_users: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                target = bound.get(func.id)
                if target in _FORBIDDEN_BARE:
                    out.append(
                        f"{rel}:{node.lineno}: call {func.id}() -> {target}"
                    )
                continue
            if not isinstance(func, ast.Attribute):
                continue
            root = func.value
            if not isinstance(root, ast.Name):
                continue
            target = bound.get(root.id)
            # ``import time`` binds the module; ``from datetime import
            # datetime`` binds the class — same call set either way.
            base = target.split(".")[0] if target else ""
            if base not in _CLOCK_MODULES:
                continue
            if func.attr in _FORBIDDEN_ATTR_CALLS.get(base, ()) or (
                func.attr in _FORBIDDEN_ZERO_ARG.get(base, ())
                and not node.args
                and not node.keywords
            ):
                out.append(f"{rel}:{node.lineno}: call {root.id}.{func.attr}()")
        elif isinstance(node, ast.Attribute) and isinstance(
            node.value, ast.Name
        ):
            base = (bound.get(node.value.id) or "").split(".")[0]
            if base in _CLOCK_MODULES and node.attr in _MEASURED_CALLS:
                measured_users.add(rel)
        elif isinstance(node, ast.Name):
            target = bound.get(node.id) or ""
            if target.split(".")[-1] in _MEASURED_CALLS and "." in target:
                measured_users.add(rel)
    return out, measured_users


def test_pure_tree_exists() -> None:
    files = _pure_files()
    assert files, "no pure modules found — the scan would pass vacuously"
    for d in PURE_DIRS:
        assert any(rel.startswith(f"{d}/") for rel, _ in files), (
            f"pure dir {d}/ contributed no modules"
        )


def test_pure_tree_has_no_network_imports() -> None:
    offenders: list[str] = []
    for rel, path in _pure_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in _FORBIDDEN_IMPORT_ROOTS:
                        offenders.append(f"{rel}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root in _FORBIDDEN_IMPORT_ROOTS:
                    offenders.append(f"{rel}: from {node.module} import ...")
    assert offenders == [], "network imports in the pure tree:\n" + "\n".join(
        offenders
    )


def test_pure_tree_has_no_wall_clock_calls() -> None:
    offenders: list[str] = []
    for rel, path in _pure_files():
        if rel in _ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        violations, _ = _clock_scan(rel, tree)
        offenders.extend(violations)
    assert offenders == [], (
        "civil-time reads in the pure tree (inject a clock instead):\n"
        + "\n".join(offenders)
    )


def test_duration_clock_users_are_documented() -> None:
    """``monotonic``/``perf_counter``/``process_time`` are the only
    allowed clocks — and only in files with a recorded exception."""
    users: set[str] = set()
    for rel, path in _pure_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        _, used = _clock_scan(rel, tree)
        users |= used
    assert users <= _ALLOWED_CLOCK_FILES, (
        "duration-clock users outside the documented exceptions:\n"
        + "\n".join(sorted(users - _ALLOWED_CLOCK_FILES))
    )
