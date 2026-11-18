"""Install lifecycle — canonical ``forge/InstallReceipt/v1`` receipts,
health status, doctor, uninstall tracking.

Two install layers, two receipt universes (never conflated):

- **account scope** — the *service runtime*: venv under
  ``~/.forge/installs/forge-lens``, launcher shim on PATH, registry
  manifest ``~/.forge/installations/forge-lens.json`` plus the
  ``.<forge>-bootstrap.json`` receipt. Written exclusively by the
  portable bootstrap (``install.sh`` → ``scripts/forge_bootstrap.py``).
  This module only OBSERVES that layer — ``install status``/``install
  doctor`` read the manifest, never write it.
- **project/workspace scope** — the per-root ``<data-dir>/`` state
  (default ``./.forge-lens``): ``config.toml`` + ``artifacts/`` plus the
  receipts this module writes. The orchestrator discovers the newest
  ``forge/InstallReceipt/v1`` under ``<root>/.forge-lens/install/
  receipts/``; ``receipt.json`` is the same document kept as the
  "latest" pointer for ``status``/``uninstall``.

A receipt is an audit trail, not proof of install: ``status`` reports
``receipt_recorded``/``unverified`` when only paper exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from forge_lens import __version__
from forge_lens.core.config import ensure_state_dir

SCHEMA_RECEIPT = "forge/InstallReceipt/v1"
SCHEMA_HEALTH = "forge/InstallationHealth/v1"
FORGE_ID = "forge-lens"
DISTRIBUTION = "forge-lens-fl"
PROFILES = ("minimal", "recommended", "full")
# Scopes this module actually installs into. "user" is the portable
# bootstrap's layer (venv+shim+registry) — declared honestly, not faked.
SCOPES = ("project", "workspace")
_SOURCES = ("pypi", "wheel", "git", "zip", "local", "gitless", "offline")
CHECK_STATUSES = ("PASS", "FAIL", "BLOCKED", "UNVERIFIED", "NOT_APPLICABLE")
RECEIPTS_DIR = "receipts"

E_APPROVAL = "FORGE-INSTALL-PLAN-NOT-APPROVED"
E_SCOPE = "FORGE-INSTALL-SCOPE-UNSUPPORTED"
E_PROFILE = "FORGE-INSTALL-PROFILE-UNKNOWN"
E_SOURCE = "FORGE-INSTALL-SOURCE-UNKNOWN"
E_WRITE = "FORGE-INSTALL-WRITE-FAILED"

# install-2.1 profile semantics — declared equivalence, not fake
# differentiation (§5.4): the lens install surface records a receipt and
# prepares local state; there are no profile-gated components, so all
# profiles install the same surface today.
PROFILE_EQUIVALENCE_NOTE = (
    "all profiles install the same surface — forge-lens ships no "
    "profile-gated components; minimal|recommended|full are recorded "
    "verbatim for orchestrator compatibility"
)

# Flags accepted for orchestrator compatibility with no install-time
# meaning for forge-lens — always recorded verbatim, never silently
# dropped and never faked into behavior.
_IGNORED_FLAG_REASONS = {
    "host": (
        "lens deploys no host-side assets at install time — the Claude "
        "adapter imports transcripts at ingest (import-claude); "
        "host_activation is NOT_APPLICABLE"
    ),
    "components": (
        "lens has no install-time component vocabulary — the install "
        "surface is state-dir + receipt only"
    ),
    "gateway_profile": (
        "lens is not an investigation runtime — no gateway depth "
        "semantics exist to select"
    ),
}

_UNINSTALL_BOUNDARY = (
    "receipt-scope uninstall only — the account runtime "
    "(~/.forge/installs/forge-lens, ~/.local/bin/forge-lens, "
    "~/.forge/installations/forge-lens.json, FORGE_LENS_* account env) "
    "is owned by the portable bootstrap and is NOT removed"
)


def _utc_now(ts_ns: int) -> datetime:
    return datetime.fromtimestamp(ts_ns / 1e9, UTC)


def _iso(ts_ns: int) -> str:
    return _utc_now(ts_ns).strftime("%Y-%m-%dT%H:%M:%SZ")


def _stamp(ts_ns: int) -> str:
    return _utc_now(ts_ns).strftime("%Y%m%dT%H%M%SZ")


def _dir(data_dir: Path, *, create: bool = True) -> Path:
    d = data_dir / "install"
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d


def _receipt_path(data_dir: Path, *, create: bool = True) -> Path:
    return _dir(data_dir, create=create) / "receipt.json"


def read_receipt(data_dir: Path) -> dict[str, Any] | None:
    p = _receipt_path(data_dir, create=False)
    if not p.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data


def write_receipt(data_dir: Path, receipt: dict[str, Any]) -> Path:
    """Write the canonical receipt to ``install/receipts/<op>-<ts>.json``
    (orchestrator-discoverable history) and refresh the
    ``install/receipt.json`` latest-pointer with the same document."""
    base = _dir(data_dir)
    history = base / RECEIPTS_DIR
    history.mkdir(parents=True, exist_ok=True)
    op = str(receipt.get("operation") or "install")
    stamp = _stamp(int(receipt.get("ts_ns") or 0))
    doc = json.dumps(receipt, indent=2, sort_keys=True)
    (history / f"{op}-{stamp}.json").write_text(doc, encoding="utf-8")
    latest = base / "receipt.json"
    latest.write_text(doc, encoding="utf-8")
    return latest


# --------------------------------------------------------------------------
# account layer — observe only, never write
# --------------------------------------------------------------------------


def _forge_home() -> Path:
    """Same home resolution as the vendored bootstrap
    (``FORGE_HOME_OVERRIDE`` then ``Path.home()``) — tests redirect the
    whole account layer through the env var."""
    return Path(os.environ.get("FORGE_HOME_OVERRIDE") or Path.home()) / ".forge"


def _account_state() -> dict[str, Any]:
    """Observe the account-scope install the portable bootstrap owns.

    ``observed``   — registry manifest parses and its install root +
                     venv python still exist.
    ``degraded``   — manifest parses but the runtime it describes is
                     partially gone (venv/launcher missing).
    ``not_installed`` — no manifest.
    ``unverified`` — a manifest path exists but is unreadable/corrupt.
    """
    manifest = _forge_home() / "installations" / f"{FORGE_ID}.json"
    out: dict[str, Any] = {
        "layer": "account",
        "owner": "scripts/forge_bootstrap.py (install.sh/install.ps1)",
        "manifest": str(manifest),
    }
    if not manifest.is_file():
        out["state"] = "not_installed"
        out["detail"] = "no registry manifest — run ./install.sh"
        return out
    try:
        doc = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        out["state"] = "unverified"
        out["detail"] = f"manifest unreadable: {exc}"
        return out
    install_root = Path(str(doc.get("install_root") or ""))
    venv = Path(str(doc.get("venv") or ""))
    venv_python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    shim = (doc.get("cli") or {}).get("shim") if isinstance(doc.get("cli"), dict) else None
    missing = [
        name
        for name, p in (("install_root", install_root), ("venv_python", venv_python))
        if not str(p) or not p.exists()
    ]
    out.update(
        {
            "state": "observed" if not missing else "degraded",
            "install_root": str(install_root),
            "venv": str(venv),
            "venv_python_present": venv_python.exists(),
            "shim": shim,
            "shim_present": Path(shim).exists() if shim else None,
            "installed_at": doc.get("installed_at"),
            "version": doc.get("version"),
            "installation_kind": doc.get("installation_kind"),
        }
    )
    if missing:
        out["detail"] = f"manifest present but runtime incomplete: missing {missing}"
    return out


# --------------------------------------------------------------------------
# canonical receipt builders
# --------------------------------------------------------------------------


def _receipt_id(fields: dict[str, Any]) -> str:
    raw = json.dumps(fields, sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _receipt(
    *,
    operation: str,
    status: str,
    scope: str,
    data_dir: Path,
    root: Path | None,
    ts_ns: int,
    profile: str | None = None,
    source: str | None = None,
    host: str | None = None,
    components: list[str] | None = None,
    gateway_profile: str | None = None,
    managed_files: list[str] | None = None,
    checks: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
    dry_run: bool = False,
    error: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = sorted(managed_files or [])
    target_root = str(root) if root is not None else str(data_dir.parent)
    doc: dict[str, Any] = {
        "schema": SCHEMA_RECEIPT,
        "receipt_id": _receipt_id(
            {"o": operation, "s": scope, "d": str(data_dir), "f": files, "t": ts_ns}
        ),
        "forge_id": FORGE_ID,
        "operation": operation,
        "status": status,
        "scope": scope,
        "profile": profile,
        "source": source,
        "target_root": target_root,
        "data_dir": str(data_dir),
        "host": host,
        "components": components,
        "gateway_profile": gateway_profile,
        "managed_files": files,
        "checks": checks or [],
        "verification": verification or {"status": "UNVERIFIED"},
        "dry_run": dry_run,
        "env": {"python": sys.version.split()[0]},
        "created_at": _iso(ts_ns),
        "created_by": f"{DISTRIBUTION}/{__version__}",
        "ts_ns": ts_ns,
    }
    if error:
        doc["error"] = error
    if extra:
        doc.update(extra)
    return doc


def _check(name: str, status: str, detail: str = "", *, required: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {"check": name, "status": status}
    if detail:
        out["detail"] = detail
    if not required:
        out["required"] = False
    return out


def _verdict(checks: list[dict[str, Any]]) -> dict[str, Any]:
    """PASS iff every *required* check is PASS/NOT_APPLICABLE — matching
    the installkit's gate. Non-required observations never promote nor
    fail the receipt."""
    required = [c for c in checks if c.get("required", True)]
    ok = all(c["status"] in ("PASS", "NOT_APPLICABLE") for c in required)
    return {"status": "PASS" if ok else "FAIL"}


def _ignored_flags(
    host: str | None, components: list[str] | None, gateway_profile: str | None
) -> list[dict[str, Any]]:
    """Flags accepted verbatim that have no install-time meaning — the
    receipt proves they were seen and honestly not applied."""
    out: list[dict[str, Any]] = []
    if host is not None:
        out.append({"flag": "--host", "value": host, "reason": _IGNORED_FLAG_REASONS["host"]})
    if components:
        out.append(
            {
                "flag": "--components",
                "value": components,
                "reason": _IGNORED_FLAG_REASONS["components"],
            }
        )
    if gateway_profile is not None:
        out.append(
            {
                "flag": "--gateway-profile",
                "value": gateway_profile,
                "reason": _IGNORED_FLAG_REASONS["gateway_profile"],
            }
        )
    return out


def _fail(
    kind: str,
    detail: str,
    *,
    receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": f"{kind}: {detail}", "error_kind": kind}
    if receipt is not None:
        out["receipt"] = receipt
    return out


# --------------------------------------------------------------------------
# operations
# --------------------------------------------------------------------------


def install(
    data_dir: Path,
    *,
    scope: str = "project",
    root: Path | None = None,
    host: str | None = None,
    profile: str = "recommended",
    source: str = "local",
    components: list[str] | None = None,
    gateway_profile: str | None = None,
    files: list[str] | None = None,
    ts_ns: int,
    dry_run: bool = False,
    approved: bool = False,
) -> dict[str, Any]:
    """Project/workspace-scope install: prepare ``<data-dir>`` state and
    write a canonical ``forge/InstallReceipt/v1``.

    ``scope=user`` is refused — the account runtime is owned by the
    portable bootstrap, and this module never writes outside data_dir.
    ``approved`` is the install-2.1 mutation gate: the CLI resolves it
    from ``--yes`` or the interactive default-No confirm; dry-run plans
    skip the gate because they write nothing.
    """
    if scope not in SCOPES:
        receipt = _receipt(
            operation="install",
            status="failed",
            scope=scope,
            data_dir=data_dir,
            root=root,
            ts_ns=ts_ns,
            dry_run=dry_run,
            error={
                "kind": E_SCOPE,
                "detail": (
                    f"scope {scope!r} is not a lens CLI install surface — the "
                    "account runtime (venv/launcher/registry) is installed by "
                    "./install.sh (scripts/forge_bootstrap.py); this verb "
                    f"manages {SCOPES} state only"
                ),
            },
        )
        return _fail(
            E_SCOPE,
            receipt["error"]["detail"],
            receipt=receipt,
        )
    if profile not in PROFILES:
        return _fail(E_PROFILE, f"profile must be one of {PROFILES}")
    if source not in _SOURCES:
        return _fail(E_SOURCE, f"source must be one of {_SOURCES}")
    if not approved and not dry_run:
        receipt = _receipt(
            operation="install",
            status="failed",
            scope=scope,
            data_dir=data_dir,
            root=root,
            ts_ns=ts_ns,
            profile=profile,
            source=source,
            host=host,
            components=components,
            gateway_profile=gateway_profile,
            error={
                "kind": E_APPROVAL,
                "detail": (
                    "install mutation requires approval — pass --yes or confirm interactively"
                ),
            },
        )
        return _fail(E_APPROVAL, receipt["error"]["detail"], receipt=receipt)

    managed = ["config.toml", *(files or [])]
    ignored = _ignored_flags(host, components, gateway_profile)
    checks: list[dict[str, Any]] = []
    if dry_run:
        checks.append(
            _check("data_dir_initialized", "NOT_APPLICABLE", "plan only — nothing written")
        )
    else:
        try:
            ensure_state_dir(data_dir, project_id="default")
        except OSError as exc:
            receipt = _receipt(
                operation="install",
                status="failed",
                scope=scope,
                data_dir=data_dir,
                root=root,
                ts_ns=ts_ns,
                profile=profile,
                source=source,
                managed_files=managed,
                error={"kind": E_WRITE, "detail": f"state dir init failed: {exc}"},
            )
            return _fail(E_WRITE, receipt["error"]["detail"], receipt=receipt)
        checks.append(
            _check(
                "data_dir_initialized",
                "PASS",
                f"{data_dir} (config.toml + artifacts/)",
            )
        )
    account = _account_state()
    checks.append(
        _check(
            "account_runtime",
            "PASS" if account["state"] == "observed" else "UNVERIFIED",
            f"{account['state']}: {account.get('detail') or account['manifest']}",
            required=False,
        )
    )
    if scope == "workspace":
        checks.append(
            _check(
                "workspace_member_discovery",
                "NOT_APPLICABLE",
                "lens tracks per-root .forge-lens state — member "
                "enumeration belongs to the orchestrator, no members probed",
            )
        )
    if host is not None:
        checks.append(
            _check("host_activation", "NOT_APPLICABLE", _IGNORED_FLAG_REASONS["host"])
        )
    if dry_run:
        verification = {"status": "UNVERIFIED", "detail": "plan only — nothing executed"}
        status_ = "planned"
    else:
        checks.append(_check("receipt_written", "PASS", "canonical receipt persisted"))
        verification = _verdict(checks)
        status_ = "completed" if verification["status"] == "PASS" else "failed"
    receipt = _receipt(
        operation="install",
        status=status_,
        scope=scope,
        data_dir=data_dir,
        root=root,
        ts_ns=ts_ns,
        profile=profile,
        source=source,
        host=host,
        components=components,
        gateway_profile=gateway_profile,
        managed_files=managed,
        checks=checks,
        verification=verification,
        dry_run=dry_run,
        extra={
            "flags_recorded_not_applicable": ignored,
            "profile_semantics": {
                "requested": profile,
                "equivalent_profiles": True,
                "note": PROFILE_EQUIVALENCE_NOTE,
            },
        },
    )
    if dry_run:
        return {"ok": True, "dry_run": True, "receipt": receipt}
    try:
        path = write_receipt(data_dir, receipt)
    except OSError as exc:
        # an unwritable data_dir is an install failure, not a crash —
        # report through the same {"ok": False} channel as bad input
        receipt["status"] = "failed"
        receipt["error"] = {"kind": E_WRITE, "detail": f"receipt write failed: {exc}"}
        return _fail(E_WRITE, f"receipt write failed: {exc}", receipt=receipt)
    return {"ok": True, "receipt": receipt, "receipt_path": str(path)}


def _project_state(data_dir: Path, receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Verify the receipt-scope layer — a receipt alone is never
    ``installed``; real state (config.toml + artifacts/) must exist."""
    op = (receipt or {}).get("operation") or (receipt or {}).get("action")
    config = (data_dir / "config.toml").is_file()
    out: dict[str, Any] = {
        "layer": "project",
        "data_dir": str(data_dir),
        "config_toml": config,
        "receipt_present": receipt is not None,
        "receipt_operation": op,
    }
    if op == "uninstall":
        out["state"] = "uninstalled"
    elif receipt is not None and op == "install" and config:
        out["state"] = "installed"
    elif receipt is not None:
        out["state"] = "receipt_recorded"
        out["detail"] = "receipt exists without the state it describes — audit trail only"
    elif data_dir.is_dir() and config:
        out["state"] = "unverified"
        out["detail"] = "state dir initialized with no install receipt"
    else:
        out["state"] = "not_installed"
    return out


def status(data_dir: Path) -> dict[str, Any]:
    """Real install health — ``installed`` is true only when a verified
    layer exists (project state dir or account runtime), never on paper
    alone."""
    receipt = read_receipt(data_dir)
    project = _project_state(data_dir, receipt)
    account = _account_state()
    history_dir = _dir(data_dir, create=False) / RECEIPTS_DIR
    receipts = (
        sorted(p.name for p in history_dir.glob("*.json")) if history_dir.is_dir() else []
    )
    installed = project["state"] == "installed" or account["state"] == "observed"
    if account["state"] == "degraded":
        state = "degraded"
    elif installed:
        state = "installed"
    elif project["state"] in ("receipt_recorded", "unverified") or account["state"] == "unverified":
        state = project["state"] if project["state"] != "not_installed" else "unverified"
    elif project["state"] == "uninstalled" and account["state"] == "not_installed":
        state = "uninstalled"
    else:
        state = "not_installed"
    return {
        "schema": SCHEMA_HEALTH,
        "forge_id": FORGE_ID,
        "installed": installed,
        "state": state,
        "project": project,
        "account": account,
        "receipt": receipt,
        "receipts": receipts,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _toml_check(data_dir: Path) -> dict[str, Any]:
    cfg = data_dir / "config.toml"
    if not cfg.is_file():
        return _check(
            "config_parseable", "NOT_APPLICABLE", "no config.toml — state not initialized"
        )
    try:
        tomllib.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return _check("config_parseable", "FAIL", str(exc))
    return _check("config_parseable", "PASS")


def doctor(data_dir: Path) -> dict[str, Any]:
    """Health check — real verification on both layers: receipt readable
    AND canonical, state dir present and writable, account runtime
    observed (informational — its absence is reported, not hidden)."""
    checks: list[dict[str, Any]] = []
    checks.append(
        _check(
            "python>=3.11",
            "PASS" if sys.version_info >= (3, 11) else "FAIL",
            sys.version.split()[0],
        )
    )
    if data_dir.is_dir():
        checks.append(_check("data_dir", "PASS", str(data_dir)))
        try:
            probe = data_dir / ".lens-probe"
            probe.write_text("ok")
            probe.unlink()
            checks.append(_check("data_dir_writable", "PASS"))
        except OSError:
            checks.append(_check("data_dir_writable", "FAIL", "probe write refused"))
    else:
        checks.append(_check("data_dir", "FAIL", f"absent: {data_dir}"))
        checks.append(
            _check("data_dir_writable", "NOT_APPLICABLE", "no data dir to probe")
        )
    checks.append(_toml_check(data_dir))
    r = read_receipt(data_dir)
    if r is None:
        checks.append(
            _check(
                "receipt",
                "UNVERIFIED",
                "no install receipt — audit trail absent",
                required=False,
            )
        )
    elif r.get("schema") == SCHEMA_RECEIPT:
        checks.append(
            _check("receipt", "PASS", f"{SCHEMA_RECEIPT} ({r.get('operation')})")
        )
    else:
        checks.append(
            _check(
                "receipt",
                "PASS",
                "legacy pre-2.1 receipt — still honored by status/uninstall",
            )
        )
    account = _account_state()
    if account["state"] == "observed":
        checks.append(
            _check(
                "account_manifest",
                "PASS",
                f"{account['manifest']} (installed_at={account.get('installed_at')})",
                required=False,
            )
        )
        checks.append(
            _check("account_venv", "PASS", account["venv"], required=False)
        )
        checks.append(
            _check(
                "account_launcher",
                "PASS" if account.get("shim_present") else "UNVERIFIED",
                str(account.get("shim") or "no shim recorded"),
                required=False,
            )
        )
    elif account["state"] == "degraded":
        checks.append(
            _check(
                "account_manifest",
                "FAIL",
                account.get("detail", "runtime incomplete"),
                required=False,
            )
        )
    else:
        checks.append(
            _check(
                "account_manifest",
                account["state"].upper() if account["state"] == "unverified" else "UNVERIFIED",
                account.get("detail", ""),
                required=False,
            )
        )
    required = [c for c in checks if c.get("required", True)]
    ok = all(c["status"] in ("PASS", "NOT_APPLICABLE") for c in required)
    return {
        "schema": SCHEMA_HEALTH,
        "forge_id": FORGE_ID,
        "ok": ok,
        "checks": checks,
        "checked_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def uninstall(data_dir: Path, *, ts_ns: int, approved: bool = False) -> dict[str, Any]:
    """Remove tracked files; write a canonical uninstall receipt (audit
    trail — the receipt itself survives so the removal is provable).

    Boundary: receipt-scope only — the account runtime (venv, launcher,
    registry manifest, account env vars) belongs to the portable
    bootstrap and is never touched here.
    """
    if not approved:
        receipt = _receipt(
            operation="uninstall",
            status="failed",
            scope="project",
            data_dir=data_dir,
            root=None,
            ts_ns=ts_ns,
            error={
                "kind": E_APPROVAL,
                "detail": (
                    "uninstall mutation requires approval — "
                    "pass --yes or confirm interactively"
                ),
            },
        )
        return _fail(E_APPROVAL, receipt["error"]["detail"], receipt=receipt)
    r = read_receipt(data_dir)
    removed: list[str] = []
    failed: list[str] = []
    if r:
        base = data_dir.resolve()
        files = r.get("managed_files") or r.get("files")
        for f in files if isinstance(files, list) else []:
            if not isinstance(f, str):
                continue  # corrupt receipt entry — skip, never crash
            fp = (data_dir / f).resolve()
            # lexical containment is bypassable via '..' — compare
            # resolved paths only
            if fp.is_relative_to(base) and fp.is_file():
                try:
                    fp.unlink()
                    removed.append(f)
                except OSError:
                    failed.append(f)  # removal denied — record, keep going
    checks = [
        _check(
            "managed_files_removed",
            "PASS" if not failed else "FAIL",
            f"removed={len(removed)} failed={len(failed)}",
        )
    ]
    receipt = _receipt(
        operation="uninstall",
        status="completed" if not failed else "failed",
        scope="project",
        data_dir=data_dir,
        root=None,
        ts_ns=ts_ns,
        managed_files=removed,
        checks=checks,
        verification=_verdict(checks),
        extra={
            "scope_boundary": _UNINSTALL_BOUNDARY,
            "prior_receipt": r,
            "failed": sorted(failed),
        },
    )
    try:
        write_receipt(data_dir, receipt)
    except OSError as exc:
        # unwritable data_dir → the audit trail can't record the removal;
        # report what happened instead of dying on a traceback
        out: dict[str, Any] = {
            "ok": False,
            "error": f"{E_WRITE}: receipt write failed: {exc}",
            "error_kind": E_WRITE,
            "removed": sorted(removed),
            "prior_receipt": r,
            "scope_boundary": _UNINSTALL_BOUNDARY,
        }
        if failed:
            out["failed"] = sorted(failed)
        return out
    out = {
        "ok": True,
        "removed": sorted(removed),
        "prior_receipt": r,
        "receipt": receipt,
        "scope_boundary": _UNINSTALL_BOUNDARY,
    }
    if failed:
        out["failed"] = sorted(failed)
    return out
