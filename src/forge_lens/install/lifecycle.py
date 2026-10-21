"""Install lifecycle — receipts, status, doctor, uninstall tracking.

Local-first: state lives in `<data_dir>/install/receipt.json`. Every
mutation writes a receipt so `status`/`doctor` can prove what happened
without trusting the caller's memory.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PROFILES = ("minimal", "recommended", "full")
_SOURCES = ("pypi", "wheel", "git", "zip", "local", "gitless", "offline")


def _dir(data_dir: Path) -> Path:
    d = data_dir / "install"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _receipt_path(data_dir: Path) -> Path:
    return _dir(data_dir) / "receipt.json"


def read_receipt(data_dir: Path) -> dict[str, Any] | None:
    p = _receipt_path(data_dir)
    if not p.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data


def write_receipt(data_dir: Path, receipt: dict[str, Any]) -> Path:
    p = _receipt_path(data_dir)
    p.write_text(json.dumps(receipt, indent=2, sort_keys=True), encoding="utf-8")
    return p


def install(
    data_dir: Path, *, profile: str, source: str,
    files: list[str], ts_ns: int, dry_run: bool = False,
) -> dict[str, Any]:
    if profile not in PROFILES:
        return {"ok": False, "error": f"profile must be one of {PROFILES}"}
    if source not in _SOURCES:
        return {"ok": False, "error": f"source must be one of {_SOURCES}"}
    receipt = {
        "action": "install", "profile": profile, "source": source,
        "files": sorted(files), "ts_ns": ts_ns,
        "python": sys.version.split()[0],
        "dry_run": dry_run,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "receipt": receipt}
    write_receipt(data_dir, receipt)
    return {"ok": True, "receipt": receipt}


def status(data_dir: Path) -> dict[str, Any]:
    r = read_receipt(data_dir)
    return {"installed": r is not None, "receipt": r}


def doctor(data_dir: Path) -> dict[str, Any]:
    """Health check — receipt readable, db dir writable, py version ok."""
    checks: list[dict[str, Any]] = []
    r = read_receipt(data_dir)
    checks.append({"check": "receipt", "ok": r is not None})
    try:
        probe = _dir(data_dir) / ".probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append({"check": "data_dir_writable", "ok": True})
    except OSError:
        checks.append({"check": "data_dir_writable", "ok": False})
    checks.append({"check": "python>=3.11", "ok": sys.version_info >= (3, 11)})
    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checks": checks}


def uninstall(data_dir: Path, *, ts_ns: int) -> dict[str, Any]:
    """Remove managed files; write an uninstall receipt (audit trail —
    the receipt itself survives so the removal is provable)."""
    r = read_receipt(data_dir)
    removed: list[str] = []
    if r:
        for f in r.get("files", []):
            fp = data_dir / f
            if fp.exists() and fp.is_file() and data_dir in fp.parents:
                fp.unlink()
                removed.append(f)
    write_receipt(data_dir, {"action": "uninstall", "ts_ns": ts_ns,
                             "removed": sorted(removed)})
    return {"removed": sorted(removed), "prior_receipt": r}
