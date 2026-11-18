"""install-2.1 surface coverage — canonical receipts, delegation argv,
approval gate, and no receipt-only soft-pass."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge_lens.cli.main import app

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the account layer (~/.forge) into tmp_path — same
    FORGE_HOME_OVERRIDE seam the vendored bootstrap honors."""
    home = tmp_path / "home"
    (home / ".forge" / "installations").mkdir(parents=True)
    monkeypatch.setenv("FORGE_HOME_OVERRIDE", str(home))
    return home


def _write_account_manifest(home: Path, *, venv: bool = True, shim: bool = True) -> Path:
    """A registry manifest shaped like the bootstrap's own register()."""
    install_root = home / ".forge" / "installs" / "forge-lens"
    venv_dir = install_root / ".venv"
    python = venv_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv:
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("# fake venv python\n", encoding="utf-8")
    shim_path = home / ".local" / "bin" / "forge-lens"
    if shim:
        shim_path.parent.mkdir(parents=True, exist_ok=True)
        shim_path.write_text("#!/bin/sh\n", encoding="utf-8")
    doc = {
        "schema": "forge/InstallationManifest/v1",
        "forge_id": "forge-lens",
        "install_root": str(install_root),
        "venv": str(venv_dir),
        "cli": {"name": "forge-lens", "shim": str(shim_path)},
        "installed_at": "2026-01-01T00:00:00Z",
        "version": "0.2.4",
    }
    manifest = home / ".forge" / "installations" / "forge-lens.json"
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    return manifest


# --------------------------------------------------------------------------
# delegation argv — the-forge's orchestrator sends this exact shape
# --------------------------------------------------------------------------


def test_delegation_argv_accepted_and_receipt_is_canonical(
    tmp_path: Path, isolated_home: Path
) -> None:
    """`install --scope ws --root /x --host all --profile p --yes` must
    never exit-2: flags without lens meaning are accepted verbatim and
    recorded as not-applied — the receipt is a valid
    forge/InstallReceipt/v1 the orchestrator can read."""
    root = tmp_path / "ws"
    root.mkdir()
    result = runner.invoke(
        app,
        [
            "install",
            "--scope",
            "workspace",
            "--root",
            str(root),
            "--host",
            "all",
            "--profile",
            "recommended",
            "--components",
            "skills,agents",
            "--gateway-profile",
            "deep",
            "--yes",
        ],
    )
    assert result.exit_code == 0, result.output
    out = json.loads(result.output)
    assert out["ok"] is True
    receipt = out["receipt"]
    # the-forge _valid_child_receipt contract
    assert receipt["schema"] == "forge/InstallReceipt/v1"
    assert receipt["forge_id"] == "forge-lens"
    assert receipt["operation"] == "install"
    assert receipt["status"] == "completed"
    assert receipt["verification"]["status"] == "PASS"
    assert receipt["scope"] == "workspace"
    assert receipt["target_root"] == str(root)
    assert receipt["host"] == "all"
    # --root mapped the data dir under the target root
    assert receipt["data_dir"] == str(root / ".forge-lens")
    # meaningless-for-lens flags recorded verbatim, honestly not applied
    ignored = {e["flag"] for e in receipt["flags_recorded_not_applicable"]}
    assert ignored == {"--host", "--components", "--gateway-profile"}
    # workspace member discovery declared, not faked
    checks = {c["check"]: c["status"] for c in receipt["checks"]}
    assert checks["workspace_member_discovery"] == "NOT_APPLICABLE"
    assert checks["host_activation"] == "NOT_APPLICABLE"
    # real state prepared at the target root
    assert (root / ".forge-lens" / "config.toml").is_file()
    # canonical receipt persisted where the orchestrator globs it
    receipts = list((root / ".forge-lens" / "install" / "receipts").glob("install-*.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["schema"] == "forge/InstallReceipt/v1"


def test_cli_install_defaults_are_project_scope(tmp_path: Path) -> None:
    d = tmp_path / ".forge-lens"
    result = runner.invoke(app, ["install", "--data-dir", str(d), "--yes"])
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)["receipt"]
    assert receipt["scope"] == "project"
    assert receipt["profile_semantics"]["equivalent_profiles"] is True


# --------------------------------------------------------------------------
# approval gate
# --------------------------------------------------------------------------


def test_install_requires_approval(tmp_path: Path) -> None:
    """No --yes on a non-TTY → named refusal, nothing written."""
    d = tmp_path / ".forge-lens"
    result = runner.invoke(app, ["install", "--data-dir", str(d)])
    assert result.exit_code == 1
    out = json.loads(result.output)
    assert out["ok"] is False
    assert out["error_kind"] == "FORGE-INSTALL-PLAN-NOT-APPROVED"
    assert out["receipt"]["status"] == "failed"
    assert out["receipt"]["error"]["kind"] == "FORGE-INSTALL-PLAN-NOT-APPROVED"
    assert not d.exists()


def test_install_no_input_never_approves(tmp_path: Path) -> None:
    d = tmp_path / ".forge-lens"
    result = runner.invoke(app, ["install", "--data-dir", str(d), "--no-input"])
    assert result.exit_code == 1
    assert not d.exists()


def test_dry_run_skips_approval_and_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    result = runner.invoke(
        app,
        ["install", "--scope", "workspace", "--root", str(root), "--dry-run"],
    )
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["dry_run"] is True
    assert out["receipt"]["status"] == "planned"
    assert out["receipt"]["verification"]["status"] == "UNVERIFIED"
    assert not (root / ".forge-lens").exists()


def test_uninstall_requires_approval(tmp_path: Path) -> None:
    d = tmp_path / ".forge-lens"
    assert runner.invoke(app, ["install", "--data-dir", str(d), "--yes"]).exit_code == 0
    blocked = runner.invoke(app, ["install", "uninstall", "--data-dir", str(d)])
    assert blocked.exit_code == 1
    assert json.loads(blocked.output)["error_kind"] == "FORGE-INSTALL-PLAN-NOT-APPROVED"
    ok = runner.invoke(app, ["install", "uninstall", "--data-dir", str(d), "--yes"])
    assert ok.exit_code == 0


# --------------------------------------------------------------------------
# honest scope boundaries
# --------------------------------------------------------------------------


def test_scope_user_is_a_named_refusal(tmp_path: Path, isolated_home: Path) -> None:
    """'user' scope is the bootstrap's layer — refused with the boundary
    spelled out, never a soft-passed receipt."""
    result = runner.invoke(
        app,
        ["install", "--scope", "user", "--root", str(tmp_path), "--yes"],
    )
    assert result.exit_code == 1
    out = json.loads(result.output)
    assert out["error_kind"] == "FORGE-INSTALL-SCOPE-UNSUPPORTED"
    assert "install.sh" in out["error"] or "forge_bootstrap" in out["error"]
    assert not (tmp_path / ".forge-lens").exists()


# --------------------------------------------------------------------------
# status — real state, never receipt-only soft-pass
# --------------------------------------------------------------------------


def test_status_does_not_soft_pass_on_receipt_only(
    tmp_path: Path, isolated_home: Path
) -> None:
    """A hand-planted receipt with no state dir must not report
    installed — receipt_recorded is the honest verdict."""
    d = tmp_path / ".forge-lens"
    (d / "install").mkdir(parents=True)
    (d / "install" / "receipt.json").write_text(
        json.dumps(
            {
                "schema": "forge/InstallReceipt/v1",
                "forge_id": "forge-lens",
                "operation": "install",
                "status": "completed",
                "managed_files": ["config.toml"],
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["install", "status", "--data-dir", str(d)])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert out["installed"] is False
    assert out["state"] == "receipt_recorded"
    assert out["project"]["state"] == "receipt_recorded"
    assert out["account"]["state"] == "not_installed"


def test_status_installed_from_project_state(tmp_path: Path, isolated_home: Path) -> None:
    d = tmp_path / ".forge-lens"
    assert runner.invoke(app, ["install", "--data-dir", str(d), "--yes"]).exit_code == 0
    out = json.loads(
        runner.invoke(app, ["install", "status", "--data-dir", str(d)]).output
    )
    assert out["installed"] is True
    assert out["project"]["state"] == "installed"
    assert out["account"]["state"] == "not_installed"  # honest: CLI can't venv-install


def test_status_observes_account_manifest(tmp_path: Path, isolated_home: Path) -> None:
    _write_account_manifest(isolated_home)
    d = tmp_path / ".forge-lens"  # never installed — no receipt at all
    out = json.loads(
        runner.invoke(app, ["install", "status", "--data-dir", str(d)]).output
    )
    assert out["installed"] is True  # account runtime observed
    assert out["account"]["state"] == "observed"
    assert out["project"]["state"] == "not_installed"


def test_status_degraded_when_account_runtime_missing(
    tmp_path: Path, isolated_home: Path
) -> None:
    _write_account_manifest(isolated_home, venv=False)
    out = json.loads(
        runner.invoke(
            app, ["install", "status", "--data-dir", str(tmp_path / ".forge-lens")]
        ).output
    )
    assert out["account"]["state"] == "degraded"
    assert out["state"] == "degraded"
    assert out["installed"] is False


def test_status_root_maps_data_dir(tmp_path: Path, isolated_home: Path) -> None:
    root = tmp_path / "ws"
    assert (
        runner.invoke(
            app, ["install", "--root", str(root), "--yes"]
        ).exit_code
        == 0
    )
    out = json.loads(
        runner.invoke(app, ["install", "status", "--root", str(root)]).output
    )
    assert out["project"]["data_dir"] == str(root / ".forge-lens")
    assert out["installed"] is True


# --------------------------------------------------------------------------
# doctor — real checks on both layers
# --------------------------------------------------------------------------


def test_install_doctor_reports_account_unverified(
    tmp_path: Path, isolated_home: Path
) -> None:
    d = tmp_path / ".forge-lens"
    assert runner.invoke(app, ["install", "--data-dir", str(d), "--yes"]).exit_code == 0
    out = json.loads(
        runner.invoke(app, ["install", "doctor", "--data-dir", str(d)]).output
    )
    checks = {c["check"]: c for c in out["checks"]}
    assert checks["receipt"]["status"] == "PASS"
    assert checks["data_dir_writable"]["status"] == "PASS"
    assert checks["config_parseable"]["status"] == "PASS"
    # account layer honestly unverified — not promoted, not failing
    assert checks["account_manifest"]["status"] == "UNVERIFIED"
    assert checks["account_manifest"]["required"] is False


def test_install_doctor_fails_on_empty_dir(tmp_path: Path, isolated_home: Path) -> None:
    d = tmp_path / ".forge-lens"  # nothing there
    result = runner.invoke(app, ["install", "doctor", "--data-dir", str(d)])
    assert result.exit_code == 1
    out = json.loads(result.output)
    checks = {c["check"]: c for c in out["checks"]}
    assert checks["data_dir"]["status"] == "FAIL"
    assert checks["receipt"]["status"] == "UNVERIFIED"


# --------------------------------------------------------------------------
# uninstall boundary
# --------------------------------------------------------------------------


def test_uninstall_receipt_scope_only(tmp_path: Path, isolated_home: Path) -> None:
    """Managed files + config removed; the account layer is untouched —
    the boundary is named in the receipt."""
    manifest = _write_account_manifest(isolated_home)
    d = tmp_path / ".forge-lens"
    assert runner.invoke(app, ["install", "--data-dir", str(d), "--yes"]).exit_code == 0
    assert (d / "config.toml").is_file()
    result = runner.invoke(app, ["install", "uninstall", "--data-dir", str(d), "--yes"])
    assert result.exit_code == 0
    out = json.loads(result.output)
    assert "config.toml" in out["removed"]
    assert not (d / "config.toml").exists()
    assert "forge_bootstrap" in out["scope_boundary"] or "bootstrap" in out["scope_boundary"]
    assert manifest.is_file()  # account layer untouched
    assert out["receipt"]["operation"] == "uninstall"
    assert out["receipt"]["schema"] == "forge/InstallReceipt/v1"
