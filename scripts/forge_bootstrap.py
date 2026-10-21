#!/usr/bin/env python3
"""Forge bootstrap — clone once, setup once, install anywhere.

Canonical source: ``the-forge/scripts/installkit/forge_bootstrap.py``.
Vendored per-Forge at ``scripts/forge_bootstrap.py``; driven by the
repo-local ``forge.json`` so the file itself is identical everywhere.

Pipeline (governed — see docs/portable-installation/):

    discover → resolve-python → create-venv → install-runtime
    → write-launcher → smoke-verify → register → report

stdlib-only, no network access beyond the explicit ``pip install`` step,
no credential reads, never touches the project's own venv.

Usage (from repo root, via setup.sh/setup.ps1 or directly):

    python scripts/forge_bootstrap.py [--repo .] [--dry-run]
        [--install-dir DIR] [--no-launcher] [--allow-download]
        [--python EXE] [--verbose]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path
from typing import Any

_BOOTSTRAP_VERSION = "1.1.0"
SCHEMA_MANIFEST = "forge/InstallationManifest/v1"
SCHEMA_RECEIPT = "forge/InstallReceipt/v1"
FORGE_HOME = ".forge"
INSTALLATIONS = "installations"
INSTALLS = "installs"
ENV_SCHEMA = "forge/InstallEnvironment/v1"
ENV_DIR_NAME = "forjas"
ENV_FILE_NAME = "env.sh"


class Refusal(Exception):
    def __init__(self, kind: str, detail: str):
        super().__init__(f"{kind}: {detail}")
        self.kind, self.detail = kind, detail


def _log(msg: str) -> None:
    print(f"[forge-bootstrap] {msg}", flush=True)


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _home() -> Path:
    return Path(os.environ.get("FORGE_HOME_OVERRIDE") or Path.home())


def forge_home() -> Path:
    return _home() / FORGE_HOME


def shim_dir() -> Path:
    return _home() / ".local" / "bin"


def _venv_python(v: Path) -> Path:
    return v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_bin(v: Path) -> Path:
    return v / ("Scripts" if os.name == "nt" else "bin")


def _cli_executable(cli: str, venv: Path) -> Path:
    """Return real venv entrypoint, not a PATH-dependent alias."""
    bindir = _venv_bin(venv)
    for name in (cli + (".exe" if os.name == "nt" else ""), cli):
        candidate = bindir / name
        if candidate.exists():
            return candidate
    return bindir / (cli + (".exe" if os.name == "nt" else ""))


def _run(cmd: list[str], timeout: int = 300, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, **kw)


# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------


def load_config(repo: Path) -> dict[str, Any]:
    """``forge.json`` at the repo root is the declarative driver."""
    cfg_path = repo / "forge.json"
    if not cfg_path.exists():
        raise Refusal(
            "FORGE-INSTALL-NO-CONFIG", f"{cfg_path} missing — run inside a Forge checkout"
        )
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise Refusal("FORGE-INSTALL-BAD-CONFIG", f"forge.json: {exc}")
    required = ("forge_id", "package", "distribution", "cli", "python")
    missing = [k for k in required if k not in cfg]
    if missing:
        raise Refusal("FORGE-INSTALL-BAD-CONFIG", f"forge.json missing keys: {missing}")
    return cfg


# --------------------------------------------------------------------------
# Stage 1-2: discover environment + resolve a compatible python
# --------------------------------------------------------------------------


def _semver_tuple(s: str) -> tuple[int, int, int]:
    parts = s.split(".")[:3]
    try:
        return tuple(int(p) for p in parts + ["0"] * (3 - len(parts)))
    except ValueError:
        return (0, 0, 0)


def _satisfies(version: str, spec: str) -> bool:
    """Tiny specifier check: comma-separated >=,>,<,<=,==,!= clauses."""
    v = _semver_tuple(version)
    for clause in spec.split(","):
        clause = clause.strip()
        for op in (">=", "<=", "!=", "==", ">", "<"):
            if clause.startswith(op):
                tgt = _semver_tuple(clause[len(op) :])
                if op == ">=" and not v >= tgt:
                    return False
                if op == "<=" and not v <= tgt:
                    return False
                if op == ">" and not v > tgt:
                    return False
                if op == "<" and not v < tgt:
                    return False
                if op == "==" and not v == tgt:
                    return False
                if op == "!=" and not v != tgt:
                    return False
                break
    return True


_VERSION_SNIPPET = "import sys;print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"


def _python_version_argv(argv: list[str]) -> str | None:
    try:
        r = _run(argv + ["-c", _VERSION_SNIPPET], timeout=15)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _python_version(exe: str) -> str | None:
    return _python_version_argv([exe])


def _resolve_executable(argv: list[str]) -> str | None:
    """Resolve ``sys.executable`` of an invocation — the ``py`` launcher
    loses the minor pin without it (``py -3.12`` reports py.exe's own
    path, not the interpreter it selected)."""
    try:
        rr = _run(argv + ["-c", "import sys;print(sys.executable)"], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return rr.stdout.strip() if rr.returncode == 0 and rr.stdout.strip() else None


def _explicit_python_argv(explicit: str) -> list[str]:
    """``--python``/``FORGE_PYTHON`` may carry an invocation with args
    (``"py -3.11"``) — same semantics as the forge-cli pipeline: the
    literal path wins when it exists on disk, otherwise split on
    whitespace. A real path containing spaces is never split."""
    trimmed = explicit.strip()
    if not re.search(r"\s", trimmed) or Path(trimmed).exists():
        return [trimmed]
    return trimmed.split()


def find_python(
    spec: str,
    explicit: str | None,
    allow_download: bool,
    log,
    provenance: dict[str, Any] | None = None,
) -> str:
    """Return a python executable satisfying ``spec``. Prefers interpreters
    already on PATH; ``uv`` (which can fetch a pinned interpreter) is used
    only with --allow-download.

    ``provenance`` (optional) is filled with the receipt ``python{}``
    fields — ``source`` is 'explicit' when the caller forced the
    interpreter (--python/FORGE_PYTHON; the caller maps that to
    'flag'/'env'), 'path' for a PATH-scan hit, 'uv' for a fetched one.
    """
    if explicit:
        argv = _explicit_python_argv(explicit)
        v = _python_version_argv(argv)
        if not v or not _satisfies(v, spec):
            raise Refusal(
                "FORGE-INSTALL-PYTHON-INCOMPATIBLE",
                f"--python {explicit}: {v or 'unusable'} does not satisfy {spec}",
            )
        if provenance is not None:
            provenance.update(
                {
                    "source": "explicit",
                    "version": v,
                    "selection_reason": f"explicit interpreter {explicit!r} satisfies {spec}",
                }
            )
        return _resolve_executable(argv) or argv[0]

    candidates: list[str] = []
    if os.name == "nt":
        for minor in range(9, 15):
            candidates.append(f"py -3.{minor}")
        candidates += ["python", "python3"]
    else:
        for minor in range(9, 15):
            candidates.append(f"python3.{minor}")
        candidates += ["python3", "python"]

    for cand in candidates:
        argv = cand.split()
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            r = _run(argv + ["-c", _VERSION_SNIPPET], timeout=15)
            v = r.stdout.strip()
            if r.returncode == 0 and _satisfies(v, spec):
                log(f"python {v} via {cand!r} satisfies {spec}")
                if provenance is not None:
                    provenance.update(
                        {
                            "source": "path",
                            "version": v,
                            "selection_reason": f"first PATH candidate {cand!r} satisfying {spec}",
                        }
                    )
                # resolve the real exe (the `py` launcher loses the minor pin)
                return _resolve_executable(argv) or exe
        except (OSError, subprocess.TimeoutExpired):
            continue

    # uv can resolve a pinned interpreter; opt-in only (network download).
    uv = shutil.which("uv")
    if uv and allow_download:
        wanted = spec.split(",")[0].lstrip(">=<")
        minor = ".".join(wanted.split(".")[:2]) or "3.12"
        log(f"no local python satisfies {spec}; `uv python install {minor}`")
        r = _run(["uv", "python", "install", minor], timeout=600)
        if r.returncode != 0:
            raise Refusal(
                "FORGE-INSTALL-PYTHON-INCOMPATIBLE",
                f"uv could not provide python {minor}: {r.stderr.strip()}",
            )
        r = _run(["uv", "python", "find", minor], timeout=15)
        if r.returncode == 0 and r.stdout.strip():
            if provenance is not None:
                provenance.update(
                    {
                        "source": "uv",
                        "version": None,
                        "selection_reason": f"uv python install {minor} (--allow-download)",
                    }
                )
            return r.stdout.strip()

    raise Refusal(
        "FORGE-INSTALL-PYTHON-INCOMPATIBLE",
        f"no interpreter on PATH satisfies {spec}; install one or "
        "re-run with --allow-download to let uv fetch it",
    )


# --------------------------------------------------------------------------
# Stage 3-4: isolated venv + install
# --------------------------------------------------------------------------


def create_venv(root: Path, python: str, log) -> Path:
    # `.venv` is the public convention. Keep reading legacy `venv` so
    # existing account installs remain upgradeable without migration.
    for candidate in (root / ".venv", root / "venv"):
        if candidate.exists() and _venv_python(candidate).exists():
            log(f"reusing venv {candidate}")
            return candidate
    venv_dir = root / ".venv"
    log(f"creating venv {venv_dir} with {python}")
    builder = venv.EnvBuilder(with_pip=True, upgrade_deps=False)
    if os.path.basename(python) not in ("python", "python3"):
        # explicit exe — delegate to it for venv creation
        r = _run([python, "-m", "venv", str(venv_dir)], timeout=300)
        if r.returncode != 0:
            raise Refusal("FORGE-INSTALL-VENV-FAILED", r.stderr.strip()[:400])
    else:
        builder.create(str(venv_dir))
    if not _venv_python(venv_dir).exists():
        raise Refusal("FORGE-INSTALL-VENV-FAILED", f"{venv_dir} incomplete")
    return venv_dir


def split_dist_spec(spec: str) -> tuple[str, str]:
    """Split ``name[extras]<constraints>`` into ``(name, rest)``.

    ``rest`` is everything after the bare distribution name — an existing
    extras bracket, a version specifier, or a wheelhouse/URL tail. Extras we
    add must be inserted between name and rest: ``name==1.0[mcp]`` is a
    specifier that matches nothing.
    """
    match = re.match(r"^([A-Za-z0-9._-]+)(.*)$", spec.strip())
    if not match:  # unreachable for a validated spec; keep refusal-shaped
        raise Refusal(
            "FORGE-INSTALL-DIST-INVALID",
            f"--dist {redact_url_userinfo(spec)!r} nao e um spec valido",
        )
    return match.group(1), match.group(2)


# `[^/\s]+` (not `[^/@\s]+`): a raw `@` *inside* userinfo must not
# truncate the redaction — `u@raw:pw@host` redacts whole, while a `@`
# after the first `/` is path content and never matches. Accepted
# trade-off: a `@` in a query without a path (`host?x=a@b`) also
# redacts — over-redacting a host in a log beats leaking a credential.
_URL_USERINFO_RE = re.compile(r"(https?://)[^/\s]+@")


def redact_url_userinfo(text: str) -> str:
    """Strip ``user:pass@`` userinfo: o spec persistido no manifesto nunca
    carrega credencial de wheelhouse (§6.5); o host/path sobrevive para
    diagnostico. ``pip install`` roda com o spec original em memoria."""
    return _URL_USERINFO_RE.sub(r"\1***@", text)


def canonical_dist_name(name: str) -> str:
    """PEP 503 normalization without a `packaging` dependency (installkit is
    stdlib-only): ``sparkforge_aws`` and ``sparkforge-aws`` are the same dist."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def compose_dist_req(dist: str, extras: list[str]) -> str:
    """Build the pip requirement for ``--dist``: extras go between the
    distribution name and the specifier, merged with any extras the caller
    already wrote in the spec — ``name[e1,e2]==v``, never ``name==v[e]``."""
    name, rest = split_dist_spec(dist)
    bracket = re.match(r"^\[([^\]]*)\](.*)$", rest)
    tail = bracket.group(2) if bracket else rest
    declared = {e.strip() for e in (bracket.group(1).split(",") if bracket else []) if e.strip()}
    merged = sorted(declared | set(extras))
    return f"{name}[{','.join(merged)}]{tail}" if merged else f"{name}{tail}"


def install_package(
    repo: Path,
    venv: Path,
    extras: list[str],
    editable: bool,
    allow_download: bool,
    log,
    dist: str | None = None,
) -> None:
    """Install the forge runtime. ``--dist <spec>`` installs the published
    distribution (PyPI or wheelhouse) with no checkout dependence; default
    prefers a wheel built from the checkout; falls back to ``pip install``
    which may hit the network for deps — allowed and reported, never silent."""
    pip = [str(_venv_python(venv)), "-m", "pip"]
    if dist:
        req = compose_dist_req(dist, extras)
        log(f"installing published distribution {redact_url_userinfo(req)}")
        r = _run(pip + ["install", "--upgrade", req], timeout=900)
        if r.returncode != 0:
            raise Refusal(
                "FORGE-INSTALL-RUNTIME-FAILED",
                f"pip install {redact_url_userinfo(req)} failed: "
                f"{redact_url_userinfo(r.stderr.strip()[-400:])}",
            )
        return
    # Editable mode does not need a wheel. Avoid creating build artifacts in
    # the source checkout and keep project/workspace directories clean.
    whl_dir = venv.parent / ".wheels"
    whl = None
    if editable:
        target = str(repo)
        log("installing editable checkout")
    else:
        log("building wheel from checkout")
        r = _run(pip + ["wheel", str(repo), "-w", str(whl_dir), "--no-deps"], timeout=600)
        if r.returncode == 0 and whl_dir.is_dir():
            wheels = sorted(whl_dir.glob("*.whl"))
            whl = wheels[-1] if wheels else None
        target = str(whl) if whl else str(repo)
        if whl:
            log(f"installing wheel {whl.name}")
        else:
            log("wheel build unavailable; installing from checkout (deps may resolve over network)")
    req = target if not extras else f"{target}[{','.join(extras)}]"
    cmd = pip + ["install", "--upgrade"]
    if editable:
        cmd.append("-e")
    cmd.append(req)
    r = _run(cmd, timeout=900)
    if r.returncode != 0:
        offline_hint = (
            "" if allow_download else (" — retry with --allow-download if deps are missing")
        )
        raise Refusal(
            "FORGE-INSTALL-RUNTIME-FAILED",
            f"pip install failed{offline_hint}: {r.stderr.strip()[-400:]}",
        )
    shutil.rmtree(whl_dir, ignore_errors=True)


# --------------------------------------------------------------------------
# Stage 5-7: launcher, registry, smoke
# --------------------------------------------------------------------------


def write_launcher(cli: str, venv: Path, no_launcher: bool, log) -> Path | None:
    if no_launcher:
        return None
    bindir = _venv_bin(venv)
    real = _cli_executable(cli, venv)
    if not real.exists():
        raise Refusal(
            "FORGE-INSTALL-CLI-MISSING", f"{cli} not in {bindir} — package script entry missing?"
        )
    d = shim_dir()
    d.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (d / f"{cli}.cmd").write_text(f'@echo off\r\n"{real}" %*\r\n')
        (d / cli).write_text(f'#!/bin/sh\nexec "{real}" "$@"\n')
        path = d / f"{cli}.cmd"
    else:
        path = d / cli
        path.write_text(f'#!/bin/sh\nexec "{real}" "$@"\n')
        path.chmod(0o755)
    log(f"launcher {path}")
    return path


# --------------------------------------------------------------------------
# account-scoped environment
# --------------------------------------------------------------------------


def env_prefix(forge_id: str) -> str:
    """Stable shell-safe prefix; e.g. spark-forge-aws -> SPARK_FORGE_AWS."""
    return re.sub(r"[^A-Z0-9]+", "_", forge_id.upper()).strip("_")


def environment_contract(
    cfg: dict[str, Any],
    repo: Path,
    install_root: Path,
    venv: Path,
    shim: Path | None,
    *,
    has_checkout: bool = True,
) -> dict[str, Any]:
    prefix = env_prefix(str(cfg["forge_id"]))
    real_cli = _cli_executable(str(cfg["cli"]), venv)
    values = {
        f"{prefix}_INSTALL_ROOT": str(install_root.resolve()),
        f"{prefix}_VENV": str(venv.resolve()),
        f"{prefix}_PYTHON": str(_venv_python(venv).resolve()),
        f"{prefix}_CLI": str(real_cli.resolve()),
    }
    if has_checkout:
        # `<FORGE>_HOME` e a raiz de CODIGO-FONTE — so existe quando ha
        # checkout real. Numa instalacao --dist o --repo e so a pasta de onde
        # o forge.json foi lido; persisti-lo como HOME criaria a variavel
        # "apontando para clone antigo" que o contrato proibe.
        values[f"{prefix}_HOME"] = str(repo.resolve())
    if shim:
        values[f"{prefix}_LAUNCHER"] = str(shim.resolve())
    aliases = {name.replace("_", "-"): value for name, value in values.items()}
    contract = {
        "schema": ENV_SCHEMA,
        "scope": "user",
        "prefix": prefix,
        "variables": values,
        "windows_aliases": aliases if os.name == "nt" else {},
        # lf-0018 — shared InstallReceipt contract: the environment block
        # records WHICH world executed the bootstrap so receipts from
        # install.sh / install.ps1 / install.windows.sh compare equal
        # modulo these fields (spec FASE 7).
        "shell": _shell_label(),
        "execution_environment": _execution_environment(),
    }
    msystem = os.environ.get("MSYSTEM")
    if msystem:
        # Present only under MSYS2/Git Bash — absent on POSIX (msystem?).
        contract["msystem"] = msystem
    return contract


def _execution_environment() -> str:
    """Coarse executor triple: 'git-bash' | 'windows' | 'posix'."""
    if _is_git_bash():
        return "git-bash"
    return "windows" if os.name == "nt" else "posix"


def _shell_label() -> str:
    """Name of the shell that launched the adapter, best-effort.
    Under Windows outside Git Bash we cannot tell cmd from PowerShell
    at this layer — 'windows' is the honest label."""
    if _is_git_bash():
        return "git-bash"
    if os.name == "nt":
        return "windows"
    return Path(os.environ.get("SHELL", "")).name.lower().removesuffix(".exe") or "posix"


def _posix_env_paths() -> tuple[Path, Path]:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    env_file = config_home / ENV_DIR_NAME / ENV_FILE_NAME
    shell = Path(os.environ.get("SHELL", "/bin/sh")).name.lower().removesuffix(".exe")
    rc_name = {"bash": ".bashrc", "zsh": ".zshrc"}.get(shell, ".profile")
    return env_file, Path.home() / rc_name


def _is_git_bash() -> bool:
    """Return whether Windows Python was launched from MSYS2/Git Bash.

    Git Bash starts native Windows Python with ``os.name == 'nt'``.  Treating
    that process as plain Windows loses the shell-local environment file, so
    installs look successful but the next Bash command cannot see variables.
    ``MSYSTEM`` is the strongest signal; ``OSTYPE`` covers older Git Bash
    launchers and keeps the check independent of the exact MSYS profile.
    """
    if os.name != "nt":
        return False
    system = os.environ.get("MSYSTEM", "").lower()
    ostype = os.environ.get("OSTYPE", "").lower()
    return system.startswith(("msys", "mingw", "ucrt", "cygwin")) or ostype.startswith(
        ("msys", "mingw", "ucrt", "cygwin")
    )


def _shell_path(value: str) -> str:
    """Render a native path usable by the current Git Bash shell."""
    if not _is_git_bash():
        return value
    converter = shutil.which("cygpath")
    if converter:
        try:
            converted = _run([converter, "-u", value], timeout=10)
        except OSError:
            converted = None
        if converted and converted.returncode == 0 and converted.stdout.strip():
            return converted.stdout.strip()
    # Git Bash accepts forward-slash native paths when cygpath is unavailable.
    return value.replace("\\", "/")


def _persist_windows(
    values: dict[str, str], aliases: dict[str, str], shim: Path | None
) -> str | None:
    import winreg

    all_values = {**values, **aliases}
    user_path = None
    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        r"Environment",
        0,
        winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE,
    ) as key:
        for name, value in all_values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
        if shim:
            current = (
                winreg.QueryValueEx(key, "Path")[0] if _registry_has_value(key, "Path") else ""
            )
            parts = [p for p in current.split(os.pathsep) if p]
            if str(shim) not in parts:
                parts.append(str(shim))
                winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, os.pathsep.join(parts))
            user_path = os.pathsep.join(parts)
    for name, value in all_values.items():
        os.environ[name] = value
    if user_path is not None:
        current_process_path = os.environ.get("PATH", "")
        if str(shim) not in current_process_path.split(os.pathsep):
            os.environ["PATH"] = str(shim) + os.pathsep + current_process_path
    return None


def _registry_has_value(key: Any, name: str) -> bool:
    import winreg

    try:
        winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return False
    return True


def _persist_posix(values: dict[str, str], shim: Path | None, shell_shim: str | None = None) -> str:
    env_file, rc_file = _posix_env_paths()
    marker_start = "# >>> forjas account environment >>>"
    marker_end = "# <<< forjas account environment <<<"
    lines = [marker_start]
    for name, value in sorted(values.items()):
        lines.append(f"export {name}={shlex.quote(value)}")
    if shim:
        path_entry = shell_shim or str(shim)
        quoted = shlex.quote(path_entry)
        lines.extend(
            [
                f"case :${{PATH:-}}: in *:{quoted}:*) ;; *) export PATH={quoted}:${{PATH:-}} ;; esac",
            ]
        )
    lines.append(marker_end)
    block = "\n".join(lines) + "\n"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    existing = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
    pattern = re.compile(rf"{re.escape(marker_start)}.*?{re.escape(marker_end)}\n?", re.DOTALL)
    # A Windows checkout path may contain backslashes; use callable
    # replacement so ``re`` never interprets them as replacement escapes.
    rendered = (
        pattern.sub(lambda _match: block, existing)
        if pattern.search(existing)
        else block + existing
    )
    _atomic_write(env_file, rendered.encode("utf-8"))
    env_file.chmod(0o600)

    source = shlex.quote(str(env_file))
    init_line = f"[ -f {source} ] && . {source}  # forjas-account-environment"
    rc = rc_file.read_text(encoding="utf-8") if rc_file.exists() else ""
    if "# forjas-account-environment" not in rc:
        rc_file.parent.mkdir(parents=True, exist_ok=True)
        rc = rc.rstrip("\n") + ("\n" if rc else "") + init_line + "\n"
        _atomic_write(rc_file, rc.encode("utf-8"))
    for name, value in values.items():
        os.environ[name] = value
    if shim:
        current = os.environ.get("PATH", "")
        path_entry = shell_shim or str(shim)
        if path_entry not in current.split(os.pathsep):
            os.environ["PATH"] = path_entry + os.pathsep + current
    return str(env_file)


def persist_environment(environment: dict[str, Any], shim: Path | None, log) -> dict[str, Any]:
    values = dict(environment["variables"])
    if os.name == "nt":
        _persist_windows(values, dict(environment.get("windows_aliases") or {}), shim)
        if _is_git_bash():
            env_file = _persist_posix(values, shim, _shell_path(str(shim)) if shim else None)
            environment["persistence"] = "HKCU\\Environment + Git Bash shell-init"
            environment["shell_init"] = env_file
            environment["shell"] = "git-bash"
            log("account environment written to HKCU\\Environment and Git Bash shell-init")
        else:
            environment["persistence"] = "HKCU\\Environment"
            environment["shell_init"] = None
            log("account environment written to HKCU\\Environment")
    else:
        env_file = _persist_posix(values, shim)
        environment["persistence"] = "shell-init"
        environment["shell_init"] = env_file
        log(f"account environment written to {env_file}")
    return environment


def _persist_environment_checked(environment: dict[str, Any], shim: Path | None, log) -> dict:
    """OS-level persistence failures become a named refusal — the receipt
    carries FORGE-ENV-PERSISTENCE-FAILED instead of a bare traceback, so
    the shell adapters can surface a stable FORGE-* code (FASE 8)."""
    try:
        return persist_environment(environment, shim, log)
    except Refusal:
        raise
    except Exception as exc:  # OSError, winreg errors, UnicodeError…
        raise Refusal(
            "FORGE-ENV-PERSISTENCE-FAILED",
            f"environment persistence failed ({type(exc).__name__}): {exc}",
        ) from exc


def managed_files(cfg: dict, install_root: Path, persist_env: bool, no_launcher: bool) -> list[str]:
    """Artifacts the install OWNS — an uninstall removes exactly this set.

    Stores the installer MUTATES without owning (the rc-file block,
    ``HKCU\\Environment`` values, PATH entries) are deliberately absent:
    the same boundary forge-cli's ``managed_artifacts`` draws, so a
    repair/uninstall reading any route's receipt removes the same class
    of artifacts and preserves user files (FASE 7).
    """
    files = {
        str(install_root),
        str(forge_home() / INSTALLATIONS / f"{cfg['forge_id']}.json"),
        str(forge_home() / INSTALLATIONS / f".{cfg['forge_id']}-bootstrap.json"),
    }
    if not no_launcher:
        files.add(str(shim_dir() / cfg["cli"]))
        if os.name == "nt":
            files.add(str(shim_dir() / f"{cfg['cli']}.cmd"))
    if persist_env and (os.name != "nt" or _is_git_bash()):
        env_file, _ = _posix_env_paths()
        files.add(str(env_file))
    return sorted(files)


# --------------------------------------------------------------------------
# Source provenance — forge-cli detect.ts semantics (A/B/C/none)
# --------------------------------------------------------------------------

CHECKSUM_FILES = ("SHA256SUMS", "checksums.sha256", ".forge-checksums.json")
_CHECKSUM_MAX_ENTRIES = 10_000


def _checksum_integrity(repo: Path) -> str:
    """'verified' | 'partial' | 'unverified' — the same verdict the
    forge-cli classifier emits: a listed entry missing or diverging
    degrades to 'partial'; no manifest → 'unverified'. Checksums never
    identify origin — they only gate integrity."""
    found = next((f for f in CHECKSUM_FILES if (repo / f).is_file()), None)
    if not found:
        return "unverified"
    entries: list[tuple[str, str]] = []
    try:
        if found.endswith(".json"):
            raw = json.loads((repo / found).read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                files = raw.get("files")
                if files is not None:
                    if not isinstance(files, list):
                        return "partial"
                    for item in files:
                        if (
                            isinstance(item, dict)
                            and isinstance(item.get("path"), str)
                            and isinstance(item.get("sha256"), str)
                        ):
                            entries.append((item["path"], item["sha256"]))
                else:
                    for name, digest in raw.items():
                        if isinstance(digest, str):
                            entries.append((name, digest))
        else:
            for line in (repo / found).read_text(encoding="utf-8").splitlines():
                m = re.match(r"^([0-9a-fA-F]{64})[ \t]+\*?(.+)$", line)
                if m:
                    entries.append((m.group(2).strip(), m.group(1).lower()))
    except (OSError, ValueError):
        return "partial"
    if not entries or len(entries) > _CHECKSUM_MAX_ENTRIES:
        return "partial"
    root = repo.resolve()
    for rel, digest in entries:
        target = (root / rel).resolve()
        try:
            delta = target.relative_to(root)
        except ValueError:
            return "partial"  # escapes the source root — never trusted
        if not str(delta) or str(delta).startswith("..") or not target.is_file():
            return "partial"
        try:
            if _sha(target.read_bytes()) != digest.lower():
                return "partial"
        except OSError:
            return "partial"
    return "verified"


def _source_provenance(repo: Path, manifest_valid: bool) -> dict[str, Any]:
    """Identity fields of the receipt — forge-cli ``detect.ts`` semantics
    so both entry points write the same truth:

    ``is_git``       .git present (dir = clone, file = worktree link)
    ``source_trust`` A = git + manifest + structure + checksums verified;
                     B = manifest + expected structure; C = manifest
                     only; none = not a Forge.
    ``integrity``    checksum-manifest verdict (never identifies origin).
    """
    try:
        git_marker = repo / ".git"
        is_git = git_marker.exists()
        is_repo = git_marker.is_dir()
    except OSError:
        is_git, is_repo = False, False
    structure_ok = (repo / "scripts" / "forge_bootstrap.py").is_file() and any(
        (repo / marker).exists()
        for marker in (
            "pyproject.toml",
            "setup.py",
            "setup.cfg",
            "requirements.txt",
            "package.json",
        )
    )
    integrity = _checksum_integrity(repo)
    if manifest_valid and structure_ok:
        trust = "A" if is_repo and integrity == "verified" else "B"
    elif manifest_valid:
        trust = "C"
    else:
        trust = "none"
    return {
        "is_git": is_git,
        "source_trust": trust,
        "manifest_valid": manifest_valid,
        "integrity": integrity,
    }


def register(
    cfg: dict,
    install_root: Path,
    venv: Path,
    version: str,
    source: dict,
    shim: Path | None,
    mcp_cmd: list[str],
    mcp_name: str | None,
    hosts: dict,
    environment: dict[str, Any] | None = None,
    installation_method: str | None = None,
) -> Path:
    doc = {
        "schema": SCHEMA_MANIFEST,
        "forge_id": cfg["forge_id"],
        "package": cfg["package"],
        "distribution": cfg["distribution"],
        "cli": {"name": cfg["cli"], "version_cmd": [cfg["cli"], "--version"]},
        "version": version,
        "install_root": str(install_root),
        "venv": str(venv),
        "python": {
            "executable": str(_venv_python(venv)),
            "version": platform.python_version(),
            "satisfies": cfg["python"],
        },
        "source": source,
        "installation_method": installation_method
        or (
            "pypi-wheel"
            if source.get("kind") == "pypi"
            else (
                "git-checkout"
                if source.get("editable", source.get("kind") == "git-checkout")
                else "wheel"
            )
        ),
        "installed_at": _now(),
        "updated_at": _now(),
        "installed_by": {"agent": "forge_bootstrap", "version": _BOOTSTRAP_VERSION},
        "hosts": hosts,
    }
    if environment:
        doc["environment"] = environment
    if shim:
        doc["cli"]["shim"] = str(shim)
    if mcp_name:
        doc["mcp"] = {"server_name": mcp_name, "command": mcp_cmd, "verified": False}
    if cfg.get("install_command"):
        # Alternate install entrypoint (argv template; {python}/{checkout}
        # resolved by the delegator) for forges whose package boundary
        # forbids an in-package install engine.
        doc["install_command"] = list(cfg["install_command"])
    path = forge_home() / INSTALLATIONS / f"{cfg['forge_id']}.json"
    _atomic_write(path, (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode())
    _log(f"registered {path}")
    return path


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def detect_hosts() -> dict[str, Any]:
    """Observe only — which host CLIs/dirs exist; never probes live."""
    out: dict[str, Any] = {}
    for host, exe in (
        ("claude", "claude"),
        ("devin", "devin"),
        ("codex", "codex"),
        ("copilot", "copilot"),
    ):
        found = shutil.which(exe)
        dirs = {
            "claude": ["~/.claude"],
            "devin": ["~/.devin", "~/.agents"],
            "codex": ["~/.codex", "~/.agents"],
            "copilot": ["~/.agents"],
        }[host]
        dir_found = [d for d in dirs if Path(os.path.expanduser(d)).exists()]
        out[host] = {
            "cli": bool(found),
            "cli_path": found,
            "config_dirs": dir_found,
            "activatable": bool(found or dir_found),
        }
    return out


def smoke(cli: str, venv: Path, version_cmd: list[str], log) -> dict[str, str]:
    exe = _cli_executable(cli, venv)
    try:
        r = _run([str(exe)] + version_cmd, timeout=30)
        # rc=0 with empty stdout is NOT a pass — a `--version` that
        # prints nothing never proved the CLI works; it would also write
        # `version: ""` into the manifest. `.splitlines()[0]` on the
        # empty list would IndexError past the Refusal path.
        lines = r.stdout.strip().splitlines()
        ok = r.returncode == 0 and bool(lines)
        ver = lines[0] if ok and lines else ""
        log(f"smoke `{cli} {' '.join(version_cmd)}` -> {ver or r.returncode}")
        return {"status": "PASS" if ok else "FAIL", "version": ver}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "FAIL", "detail": str(exc)}


def write_receipt(cfg: dict, receipt: dict) -> Path:
    path = forge_home() / INSTALLATIONS / f".{cfg['forge_id']}-bootstrap.json"
    _atomic_write(path, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    return path


# --------------------------------------------------------------------------
# orchestrate
# --------------------------------------------------------------------------


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo", default=".", help="forge checkout root")
    ap.add_argument("--install-dir", default=None, help="override ~/.forge/installs/<forge_id>")
    ap.add_argument("--python", default=None)
    ap.add_argument(
        "--editable", action="store_true", help="pip install -e (dev mode; ties CLI to checkout)"
    )
    ap.add_argument(
        "--dist",
        default=None,
        metavar="SPEC",
        help="install a published distribution (e.g. 'sparkforge-aws==1.2.3') "
        "from PyPI or a configured wheelhouse — end-user path, no checkout dependence",
    )
    ap.add_argument(
        "--persist-env",
        action="store_true",
        help="publish account-scoped HOME/VENV/PYTHON/CLI variables",
    )
    ap.add_argument("--extras", default="", help="comma-separated extras")
    ap.add_argument("--no-launcher", action="store_true")
    ap.add_argument(
        "--allow-download",
        action="store_true",
        help="let uv fetch a pinned python; pip may use network",
    )
    ap.add_argument(
        "--allow-url",
        action="store_true",
        help="permit PEP 508 direct references in --dist (name @ URL/wheelhouse). "
        "The name check still applies, but the URL itself is passed to pip "
        "verbatim — opt-in because it fetches code from an arbitrary origin",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    def log(m: str) -> None:
        if not args.quiet:
            _log(m)

    repo = Path(args.repo).resolve()
    try:
        cfg = load_config(repo)
    except Refusal as exc:
        print(
            json.dumps(
                {"status": "failed", "error": {"kind": exc.kind, "detail": exc.detail}}, indent=2
            )
        )
        return 2

    extras = [e.strip() for e in args.extras.split(",") if e.strip()] or cfg.get("extras", [])
    install_root = (
        Path(args.install_dir) if args.install_dir else forge_home() / INSTALLS / cfg["forge_id"]
    ).resolve()
    if args.dist is not None and not args.dist.strip():
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "kind": "FORGE-INSTALL-DIST-INVALID",
                        "detail": "--dist recebeu um spec vazio",
                    },
                },
                indent=2,
            )
        )
        return 2
    if args.dist and args.editable:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "kind": "FORGE-INSTALL-MODE-CONFLICT",
                        "detail": "--dist e --editable sao mutuamente exclusivos",
                    },
                },
                indent=2,
            )
        )
        return 2
    if args.dist:
        if "@" in args.dist and not args.allow_url:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": {
                            "kind": "FORGE-INSTALL-DIST-URL",
                            "detail": (
                                f"--dist {redact_url_userinfo(args.dist)!r} usa referencia "
                                "direta a URL; repita com --allow-url se a origem for confiavel"
                            ),
                        },
                    },
                    indent=2,
                )
            )
            return 2
        try:
            spec_name, _ = split_dist_spec(args.dist)
        except Refusal as exc:
            print(
                json.dumps(
                    {"status": "failed", "error": {"kind": exc.kind, "detail": exc.detail}},
                    indent=2,
                )
            )
            return 2
        if canonical_dist_name(spec_name) != canonical_dist_name(cfg["distribution"]):
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": {
                            "kind": "FORGE-INSTALL-DIST-MISMATCH",
                            "detail": (
                                f"--dist {redact_url_userinfo(args.dist)!r} nao corresponde a "
                                f"distribution={cfg['distribution']!r} do forge.json"
                            ),
                        },
                    },
                    indent=2,
                )
            )
            return 2
    source: dict[str, Any] = (
        {
            "kind": "pypi",
            "spec": redact_url_userinfo(compose_dist_req(args.dist, extras)),
        }
        if args.dist
        else {
            "kind": "git-checkout",
            "path": str(repo),
            "ref": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
            "rev": _git(repo, "rev-parse", "HEAD"),
            "editable": bool(args.editable),
        }
    )

    # How the runtime enters the venv — pypi-wheel (--dist), git-checkout
    # (--editable) or wheel (built from the checkout). This is the value
    # the manifest stores as `installation_method`; the receipt keeps the
    # contract name `install_method`. Naming map (lf-0018): forge-cli's
    # LocalInstallReceipt uses `method`/`install_method` for the CHANNEL
    # ('local'|'pypi') while here it is the RUNTIME MODE — same field
    # name where the semantics match, documented so neither side renames
    # persisted state.
    install_method = "pypi-wheel" if args.dist else "git-checkout" if args.editable else "wheel"

    report = {
        "schema": SCHEMA_RECEIPT,
        "forge_id": cfg["forge_id"],
        "operation": "install",
        "scope": "user",
        "target_root": str(install_root),
        "platform": {
            "os": platform.system(),
            "arch": platform.machine(),
            "python": platform.python_version(),
        },
        # forge-cli parity fields — the classifier verdict the bootstrap
        # derives itself (load_config already proved the manifest).
        **_source_provenance(repo, manifest_valid=True),
        "install_method": install_method,
        # The managed set is deterministic — emit it up-front so even a
        # failed receipt tells an uninstall exactly what may be removed.
        "managed_files": managed_files(cfg, install_root, args.persist_env, args.no_launcher),
        "stages": [],
        "status": "failed",
        "created_at": _now(),
    }

    def stage(name: str, fn) -> Any:
        if args.dry_run:
            report["stages"].append({"stage": name, "status": "planned"})
            return None
        try:
            out = fn()
            report["stages"].append({"stage": name, "status": "done"})
            return out
        except Refusal as exc:
            report["stages"].append(
                {"stage": name, "status": "refused", "kind": exc.kind, "detail": exc.detail}
            )
            raise

    try:
        hosts = detect_hosts()
        report["hosts_detected"] = hosts
        # FORGE_PYTHON is the explicit-selection env var of the lf-0017
        # pipeline — --python still wins; both are validated by
        # find_python (receipt source: 'flag' | 'env' | 'path' | 'uv').
        explicit_python = args.python or os.environ.get("FORGE_PYTHON") or None
        python_prov: dict[str, Any] = {}
        python = stage(
            "resolve-python",
            lambda: find_python(
                cfg["python"], explicit_python, args.allow_download, log, python_prov
            ),
        )
        if python_prov.get("source") == "uv":
            py_source = "uv"
        elif args.python:
            py_source = "flag"
        elif os.environ.get("FORGE_PYTHON"):
            py_source = "env"
        else:
            py_source = "path"
        report["python"] = {
            "source": py_source,
            "version": python_prov.get("version") or (_python_version(python) if python else None),
            "executable": python,
            "selection_reason": python_prov.get("selection_reason")
            or (
                "--python flag"
                if args.python
                else (
                    "FORGE_PYTHON environment variable"
                    if os.environ.get("FORGE_PYTHON")
                    else "PATH scan"
                )
            ),
        }
        venv_dir = stage("create-venv", lambda: create_venv(install_root, python, log))
        stage(
            "install-runtime",
            lambda: install_package(
                repo, venv_dir, extras, args.editable, args.allow_download, log, dist=args.dist
            ),
        )
        shim = stage(
            "write-launcher", lambda: write_launcher(cfg["cli"], venv_dir, args.no_launcher, log)
        )
        planned_venv = venv_dir or (install_root / ".venv")
        planned_shim = shim or (shim_dir() / cfg["cli"])
        environment = environment_contract(
            cfg,
            repo,
            install_root,
            planned_venv,
            planned_shim,
            has_checkout=not args.dist,
        )
        if args.persist_env:
            if args.dry_run:
                report["stages"].append({"stage": "persist-environment", "status": "planned"})
            else:
                environment = stage(
                    "persist-environment",
                    lambda: _persist_environment_checked(environment, shim, log),
                )
        report["environment"] = environment
        sm = stage("smoke", lambda: smoke(cfg["cli"], venv_dir, ["--version"], log))
        version = (sm or {}).get("version", cfg.get("version", "0.0.0"))
        # Smoke ANTES do register: um install que não prova `--version`
        # nunca deixa manifesto no registry (recusa → receipt de falha,
        # sem installations/<id>.json — o lifecycle não o veria saudável).
        if sm and sm.get("status") != "PASS":
            # Named FORGE-INSTALL-VERIFY-FAILED — equivalent to
            # forge-cli's FORGE-INSTALL-VERIFICATION-FAILED (same failure
            # class, pre-existing spelling; renaming would break
            # persisted-receipt readers).
            raise Refusal("FORGE-INSTALL-VERIFY-FAILED", f"smoke failed: {sm}")
        stage(
            "register",
            lambda: register(
                cfg,
                install_root,
                venv_dir,
                version,
                source,
                shim,
                cfg.get("mcp_command", []),
                cfg.get("mcp_server_name"),
                hosts,
                environment,
                # Metodo = como o runtime entrou no venv: wheel publicada
                # (pypi-wheel), wheel construida do checkout (wheel) ou
                # editable presa ao checkout (git-checkout).
                installation_method=install_method,
            ),
        )
        report["status"] = "planned" if args.dry_run else "completed"
        report["cli"] = cfg["cli"]
        report["shim_dir"] = str(shim_dir())
        report["install_root"] = str(install_root)
        guide = _path_guidance()
        if guide:
            report["path_guidance"] = guide
            log(f"NOTE: {guide}")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except Refusal as exc:
        report["error"] = {"kind": exc.kind, "detail": exc.detail}
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    finally:
        if not args.dry_run:
            write_receipt(cfg, report)


def _git(repo: Path, *a) -> str | None:
    try:
        r = _run(["git", "-C", str(repo), *a], timeout=10)
        return r.stdout.strip() if r.returncode == 0 else None
    except OSError:
        return None


def _path_guidance() -> str | None:
    d = str(shim_dir())
    if d in os.environ.get("PATH", "").split(os.pathsep):
        return None
    if _is_git_bash():
        env_file, _ = _posix_env_paths()
        return f"source {shlex.quote(str(env_file))}  # reload Git Bash environment"
    if os.name == "nt":
        return f'setx PATH "%PATH%;{d}"  # open a new terminal after'
    sh = Path(os.environ.get("SHELL", "/bin/sh")).name
    rc = {"zsh": "~/.zshrc", "bash": "~/.bashrc"}.get(sh, "~/.profile")
    return f"echo 'export PATH=\"{d}:$PATH\"' >> {rc}  # restart the shell"


if __name__ == "__main__":
    raise SystemExit(run())
