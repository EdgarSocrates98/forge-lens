"""Ready-to-mount CLI surfaces for the shared Forge obsidian verbs.

Every Forge gets the same seven verbs with the same JSON envelopes —
``status``, ``scan``, ``preview``, ``sync-plan``, ``export``,
``export-note`` and ``history`` — so hosts and agents learn the contract
once.  Two mounts cover the ecosystem: :func:`obsidian_app` returns a
``typer.Typer`` sub-app; :func:`obsidian_argparse` wires an ``argparse``
sub-parser collection.  The vault root is operator config only:
``<FORGE>_OBSIDIAN_VAULT`` first, then the shared
``FORGE_OBSIDIAN_VAULT``; it is never accepted as a CLI/web argument.

``typer`` is imported lazily inside :func:`obsidian_app` so the neutral
package keeps its dependency-light contract for forges that only consume
the engine or mount the argparse surface.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .notes import forge_note_path, moc_note
from .obsidian import (
    ObsidianVaultError,
    export_vault_document,
    import_vault_document,
    list_obsidian_receipts,
    obsidian_projection_metadata,
    plan_vault_sync,
    scan_vault_documents,
)


def _env_prefix(forge_id: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", forge_id.upper()).strip("_")


def configured_vault(forge_id: str) -> Path | None:
    """Resolve the operator-configured vault for one forge.

    ``<FORGE>_OBSIDIAN_VAULT`` wins so a forge can have its own vault;
    the shared ``FORGE_OBSIDIAN_VAULT`` is the ecosystem default.
    """
    specific = os.environ.get(f"{_env_prefix(forge_id)}_OBSIDIAN_VAULT", "").strip()
    shared = os.environ.get("FORGE_OBSIDIAN_VAULT", "").strip()
    raw = specific or shared
    return Path(raw).expanduser() if raw else None


def vault_status(forge_id: str) -> dict[str, Any]:
    """Body-free ``theforge/ObsidianVaultDescriptor/v1``-shaped status."""
    vault = configured_vault(forge_id)
    descriptor: dict[str, Any] = {
        "schema": "theforge/ObsidianVaultDescriptor/v1",
        "version": "v1",
        "forge_id": forge_id,
        "configured": vault is not None,
        "detected": bool(vault and vault.is_dir()),
        "capabilities": [],
        "env": {
            "shared": "FORGE_OBSIDIAN_VAULT",
            "specific": f"{_env_prefix(forge_id)}_OBSIDIAN_VAULT",
            "specific_set": bool(
                os.environ.get(f"{_env_prefix(forge_id)}_OBSIDIAN_VAULT", "").strip()
            ),
        },
        "warnings": [],
        "unresolved": [],
    }
    if vault is None:
        descriptor["state"] = "unconfigured"
        descriptor["unresolved"].append(
            f"set FORGE_OBSIDIAN_VAULT (or {_env_prefix(forge_id)}_OBSIDIAN_VAULT) to enable the vault"
        )
        return descriptor
    descriptor["vault_id"] = vault.name
    if not vault.is_dir():
        descriptor["state"] = "missing"
        descriptor["unresolved"].append("configured vault path is not a directory")
        return descriptor
    descriptor["state"] = "ready"
    descriptor["capabilities"] = ["scan", "preview", "sync-plan", "export", "export-note", "history"]
    try:
        receipts = list_obsidian_receipts(vault, limit=100)
    except ObsidianVaultError:
        receipts = []
    descriptor["receipts"] = len(receipts)
    descriptor["own_notes"] = sum(1 for r in receipts if r.get("source") == forge_id)
    return descriptor


class VaultNotConfiguredError(RuntimeError):
    """Raised when no vault is configured — surfaces as a named state."""


def _require_vault(forge_id: str) -> Path:
    vault = configured_vault(forge_id)
    if vault is None:
        raise VaultNotConfiguredError(
            f"vault not configured — set FORGE_OBSIDIAN_VAULT "
            f"(or {_env_prefix(forge_id)}_OBSIDIAN_VAULT); the CLI never "
            "accepts the vault root as an argument"
        )
    return vault


def _read_candidate(file: Path | str) -> str:
    try:
        return Path(file).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise VaultNotConfiguredError(f"candidate file is not readable UTF-8: {exc}") from exc


def _ensure_parent(vault: Path, rel_path: str) -> None:
    """Create the in-vault parent dir for an export — a deliberate CLI act.

    The engine conservatively refuses writes under missing directories; the
    CLI owns that decision instead, after proving the target stays inside
    the authorized vault.  The engine still re-validates every path itself.
    """
    root = vault.resolve()
    target = (root / rel_path.replace("\\", "/")).resolve()
    if root != target and root not in target.parents:
        raise VaultNotConfiguredError("export path escapes the authorized vault")
    target.parent.mkdir(parents=True, exist_ok=True)


# -- verb payloads (shared by both mounts) ------------------------------------


def verb_status(forge_id: str, **_kw: Any) -> dict[str, Any]:
    return vault_status(forge_id)


def verb_scan(forge_id: str, *, prefix: str = "", limit: int = 200, **_kw: Any) -> dict[str, Any]:
    return scan_vault_documents(_require_vault(forge_id), prefix=prefix, limit=limit)


def verb_preview(forge_id: str, *, path: str, **_kw: Any) -> dict[str, Any]:
    imported = import_vault_document(_require_vault(forge_id), path)
    return {
        "schema": "theforge/ObsidianPreview/v1",
        "path": imported["path"],
        "title": imported["title"],
        "content_hash": imported["content_hash"],
        "size": imported["size"],
        "projection": obsidian_projection_metadata(imported["markdown"]),
        "provenance": imported["provenance"],
    }


def verb_sync_plan(
    forge_id: str,
    *,
    path: str,
    file: str | Path,
    baseline_hash: str | None = None,
    classification: str = "internal",
    source: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    return plan_vault_sync(
        _require_vault(forge_id),
        path,
        _read_candidate(file),
        baseline_hash=baseline_hash,
        classification=classification,
        source=source or forge_id,
    )


def _refresh_moc(vault: Path) -> dict[str, Any] | None:
    try:
        receipts = list_obsidian_receipts(vault, limit=100)
    except ObsidianVaultError:
        return None
    note_paths = [
        {
            "source": str(r.get("source") or ""),
            "path": str(r.get("path") or ""),
            "title": str(r.get("title") or ""),
        }
        for r in receipts
    ]
    if not note_paths:
        return None
    index = vault / "forges" / "index.md"
    baseline: str | None = None
    if index.is_file():
        import hashlib

        baseline = hashlib.sha256(index.read_bytes()).hexdigest()
    try:
        return export_vault_document(
            vault,
            "forges/index.md",
            moc_note(note_paths),
            baseline_hash=baseline,
            classification="internal",
            source="forge-obsidian",
        )
    except ObsidianVaultError as exc:
        return {"state": "skipped", "code": exc.code, "message": str(exc)}


def verb_export(
    forge_id: str,
    *,
    path: str,
    file: str | Path,
    baseline_hash: str | None = None,
    classification: str = "internal",
    yes: bool = False,
    moc: bool = True,
    source: str | None = None,
    **_kw: Any,
) -> dict[str, Any]:
    if not yes:
        return {
            "schema": "theforge/ObsidianExportRefusal/v1",
            "state": "refused",
            "reason": "export mutates the vault; re-run with --yes after reviewing `obsidian sync-plan`",
            "path": path,
        }
    vault = _require_vault(forge_id)
    _ensure_parent(vault, path)
    receipt = export_vault_document(
        vault,
        path,
        _read_candidate(file),
        baseline_hash=baseline_hash,
        classification=classification,
        source=source or forge_id,
    )
    if moc and receipt.get("sync_state") in {"created", "updated"}:
        receipt = dict(receipt)
        receipt["moc"] = _refresh_moc(vault)
    return receipt


def verb_export_note(
    forge_id: str,
    *,
    name: str,
    file: str | Path,
    **kw: Any,
) -> dict[str, Any]:
    """Export FILE as ``forges/<forge-id>/<name>.md`` — the canonical layout."""
    return verb_export(forge_id, path=forge_note_path(forge_id, name), file=file, **kw)


def verb_history(forge_id: str, *, limit: int = 50, **_kw: Any) -> dict[str, Any]:
    return {
        "schema": "theforge/ObsidianReceiptHistory/v1",
        "forge_id": forge_id,
        "items": list_obsidian_receipts(_require_vault(forge_id), limit=limit),
    }


VERBS: dict[str, Callable[..., dict[str, Any]]] = {
    "status": verb_status,
    "scan": verb_scan,
    "preview": verb_preview,
    "sync-plan": verb_sync_plan,
    "export": verb_export,
    "export-note": verb_export_note,
    "history": verb_history,
}


def verb_payload(forge_id: str, verb: str, **kwargs: Any) -> dict[str, Any]:
    """Run a verb and return its payload; failures become error payloads.

    For CLIs whose convention is ``func(args) -> dict`` (argparse + emit
    layers) rather than print-and-exit.
    """
    try:
        return VERBS[verb](forge_id, **kwargs)
    except ObsidianVaultError as exc:
        return {
            "schema": "theforge/ObsidianError/v1",
            "code": exc.code,
            "message": str(exc),
            "path": getattr(exc, "path", None),
        }
    except VaultNotConfiguredError as exc:
        return {
            "schema": "theforge/ObsidianError/v1",
            "code": "OBSIDIAN-UNCONFIGURED",
            "message": str(exc),
        }
    except Exception as exc:
        return {
            "schema": "theforge/ObsidianError/v1",
            "code": "OBSIDIAN-INTERNAL",
            "message": f"{type(exc).__name__}: {exc}",
        }


def run_verb(
    forge_id: str,
    verb: str,
    *,
    emit: Callable[[str], None] = print,
    **kwargs: Any,
) -> int:
    """Execute one verb, emit its JSON payload, return a process exit code.

    Named failures (``ObsidianVaultError``, missing vault, refusals) are
    payloads, not tracebacks — exit 2 means a refusal/error payload was
    emitted, 1 an unexpected failure, 0 success.
    """
    try:
        payload = VERBS[verb](forge_id, **kwargs)
        rc = 2 if payload.get("state") in {"refused", "failed"} else 0
    except ObsidianVaultError as exc:
        payload = {
            "schema": "theforge/ObsidianError/v1",
            "code": exc.code,
            "message": str(exc),
            "path": getattr(exc, "path", None),
        }
        rc = 2
    except VaultNotConfiguredError as exc:
        payload = {
            "schema": "theforge/ObsidianError/v1",
            "code": "OBSIDIAN-UNCONFIGURED",
            "message": str(exc),
        }
        rc = 2
    except Exception as exc:  # unexpected — honesty requires a payload too
        payload = {
            "schema": "theforge/ObsidianError/v1",
            "code": "OBSIDIAN-INTERNAL",
            "message": f"{type(exc).__name__}: {exc}",
        }
        rc = 1
    emit(json.dumps(payload, ensure_ascii=False, indent=2))
    return rc


# -- argparse mount ------------------------------------------------------------


def obsidian_argparse(
    subparsers: Any,
    forge_id: str,
    *,
    source: str | None = None,
    help_text: str = "Authorized Obsidian vault operations",
) -> Any:
    """Wire ``obsidian {status,scan,preview,sync-plan,export,export-note,history}``.

    ``subparsers`` is the parent ``add_subparsers()`` collection.  Every
    leaf sets ``func(args) -> int`` and prints a JSON envelope.
    """
    src = source or forge_id
    top = subparsers.add_parser("obsidian", help=help_text)
    sub = top.add_subparsers(dest="obsidian_command", required=True)

    def _mk(verb: str, **fixed: Any) -> Callable[[Any], int]:
        def _run(args: Any) -> int:
            kw = {k: v for k, v in vars(args).items() if k not in {"command", "obsidian_command", "func"}}
            kw.update(fixed)
            kw.setdefault("source", src)
            return run_verb(forge_id, verb, **kw)

        return _run

    p = sub.add_parser("status", help="Report configured/detected vault state.")
    p.set_defaults(func=_mk("status"))

    p = sub.add_parser("scan", help="List Markdown paths without reading bodies.")
    p.add_argument("--prefix", default="")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=_mk("scan"))

    p = sub.add_parser("preview", help="Metadata + projection, never the body.")
    p.add_argument("path")
    p.set_defaults(func=_mk("preview"))

    for verb in ("sync-plan", "export"):
        p = sub.add_parser(
            verb,
            help=(
                "Dry-run a sync of FILE into PATH."
                if verb == "sync-plan"
                else "Apply a reviewed sync plan."
            ),
        )
        p.add_argument("path")
        p.add_argument("file")
        p.add_argument("--baseline-hash", dest="baseline_hash", default=None)
        p.add_argument("--classification", default="internal")
        if verb == "export":
            p.add_argument("--yes", action="store_true", help="Confirm the reviewed plan")
            p.add_argument("--no-moc", dest="moc", action="store_false", default=True)
        p.set_defaults(func=_mk(verb))

    p = sub.add_parser("export-note", help="Export FILE as forges/<forge-id>/<name>.md")
    p.add_argument("name")
    p.add_argument("file")
    p.add_argument("--baseline-hash", dest="baseline_hash", default=None)
    p.add_argument("--classification", default="internal")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-moc", dest="moc", action="store_false", default=True)
    p.set_defaults(func=_mk("export-note"))

    p = sub.add_parser("history", help="List Forge sync receipts (body-free).")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=_mk("history"))
    return top


# -- typer mount ---------------------------------------------------------------


def obsidian_app(
    forge_id: str,
    *,
    source: str | None = None,
    moc: bool = True,
    help_text: str = "Authorized Obsidian vault operations",
) -> Any:
    """Build the shared ``<cli> obsidian`` Typer sub-application."""
    import typer

    src = source or forge_id
    app = typer.Typer(help=help_text, no_args_is_help=True)

    def _run(verb: str, **kwargs: Any) -> None:
        kwargs.setdefault("source", src)
        kwargs.setdefault("moc", moc)
        rc = run_verb(forge_id, verb, emit=typer.echo, **kwargs)
        if rc:
            raise typer.Exit(rc)

    @app.command("status")
    def status() -> None:
        """Report configured/detected vault state (never fabricates)."""
        _run("status")

    @app.command("scan")
    def scan(
        prefix: str = typer.Option("", "--prefix", help="Vault subdirectory to list"),
        limit: int = typer.Option(200, "--limit", min=1, max=10_000),
    ) -> None:
        """List Markdown paths in the vault without reading bodies."""
        _run("scan", prefix=prefix, limit=limit)

    @app.command("preview")
    def preview(path: str) -> None:
        """Preview one vault note: metadata + projection, never the body."""
        _run("preview", path=path)

    @app.command("sync-plan")
    def sync_plan(
        path: str,
        file: Path,
        baseline_hash: str | None = typer.Option(None, "--baseline-hash"),
        classification: str = typer.Option("internal", "--classification"),
    ) -> None:
        """Dry-run a sync of local FILE into the vault at PATH."""
        _run("sync-plan", path=path, file=file, baseline_hash=baseline_hash, classification=classification)

    @app.command("export")
    def export(
        path: str,
        file: Path,
        baseline_hash: str | None = typer.Option(None, "--baseline-hash"),
        classification: str = typer.Option("internal", "--classification"),
        yes: bool = typer.Option(False, "--yes", help="Confirm the reviewed sync plan"),
        no_moc: bool = typer.Option(False, "--no-moc", help="Do not refresh forges/index.md"),
    ) -> None:
        """Apply a reviewed sync plan and emit the real vault receipt."""
        _run("export", path=path, file=file, baseline_hash=baseline_hash,
             classification=classification, yes=yes, moc=not no_moc)

    @app.command("export-note")
    def export_note(
        name: str,
        file: Path,
        baseline_hash: str | None = typer.Option(None, "--baseline-hash"),
        classification: str = typer.Option("internal", "--classification"),
        yes: bool = typer.Option(False, "--yes", help="Confirm the reviewed sync plan"),
        no_moc: bool = typer.Option(False, "--no-moc"),
    ) -> None:
        """Export FILE as ``forges/<forge-id>/<name>.md`` — canonical layout."""
        _run("export-note", name=name, file=file, baseline_hash=baseline_hash,
             classification=classification, yes=yes, moc=not no_moc)

    @app.command("history")
    def history(limit: int = typer.Option(50, "--limit", min=1, max=100)) -> None:
        """List the vault's Forge sync receipts (body-free ledger)."""
        _run("history", limit=limit)

    return app


def mount_obsidian(app: Any, forge_id: str, **kwargs: Any) -> Any:
    """Convenience: ``app.add_typer(obsidian_app(forge_id), name='obsidian')``."""
    app.add_typer(obsidian_app(forge_id, **kwargs), name="obsidian")
    return app


__all__ = [
    "VERBS",
    "VaultNotConfiguredError",
    "configured_vault",
    "forge_note_path",
    "moc_note",
    "mount_obsidian",
    "obsidian_app",
    "obsidian_argparse",
    "run_verb",
    "vault_status",
    "verb_payload",
]
