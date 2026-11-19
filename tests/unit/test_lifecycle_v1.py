"""cycle-16.1 wave-10 — ``forge/InstallLifecycle/v1`` envelope on
receipts/health docs, incremental reinstall hash-compare (§7.4), and
honest context metadata / host-surface sync for ``forge.agentic.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from forge_lens.cli.main import app
from forge_lens.install import lifecycle

runner = CliRunner()

REPO = Path(__file__).resolve().parents[2]

_LIFECYCLE_STATES = {
    "planned",
    "blocked",
    "failed",
    "applied",
    "partially_applied",
    "verified",
    "unverified",
    "ready",
    "certified",
}


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    (home / ".forge" / "installations").mkdir(parents=True)
    monkeypatch.setenv("FORGE_HOME_OVERRIDE", str(home))
    return home


def _install(d: Path, *extra: str):
    return runner.invoke(
        app, ["install", "--data-dir", str(d), "--yes", *extra]
    )


# --------------------------------------------------------------------------
# forge/InstallLifecycle/v1 — derived envelope on every emitted document
# --------------------------------------------------------------------------


def test_install_receipt_carries_lifecycle_verified(
    tmp_path: Path, isolated_home: Path
) -> None:
    d = tmp_path / ".forge-lens"
    result = _install(d)
    assert result.exit_code == 0, result.output
    receipt = json.loads(result.output)["receipt"]
    lc = receipt["lifecycle"]
    assert lc["schema"] == "forge/InstallLifecycle/v1"
    assert lc["state"] in _LIFECYCLE_STATES
    assert lc["state"] == "verified"  # completed + verification PASS
    # the persisted receipt carries the same envelope
    stored = json.loads((d / "install" / "receipt.json").read_text())
    assert stored["lifecycle"]["state"] == "verified"


def test_planned_receipt_derives_planned(tmp_path: Path) -> None:
    d = tmp_path / ".forge-lens"
    out = json.loads(
        runner.invoke(app, ["install", "--data-dir", str(d), "--dry-run"]).output
    )
    assert out["receipt"]["lifecycle"]["state"] == "planned"


def test_gate_refusals_derive_blocked(tmp_path: Path) -> None:
    """Approval/scope refusals are ``blocked``, never ``failed`` — the
    BLOCKED check on the receipt is what derives it."""
    d = tmp_path / ".forge-lens"
    out = json.loads(runner.invoke(app, ["install", "--data-dir", str(d)]).output)
    assert out["error_kind"] == "FORGE-INSTALL-PLAN-NOT-APPROVED"
    assert out["receipt"]["lifecycle"]["state"] == "blocked"
    checks = {c["check"]: c["status"] for c in out["receipt"]["checks"]}
    assert checks["approval_gate"] == "BLOCKED"

    out = json.loads(
        runner.invoke(
            app, ["install", "--data-dir", str(d), "--scope", "user", "--yes"]
        ).output
    )
    assert out["error_kind"] == "FORGE-INSTALL-SCOPE-UNSUPPORTED"
    assert out["receipt"]["lifecycle"]["state"] == "blocked"


def test_uninstall_refusal_and_success_lifecycle(
    tmp_path: Path, isolated_home: Path
) -> None:
    d = tmp_path / ".forge-lens"
    assert _install(d).exit_code == 0
    blocked = json.loads(
        runner.invoke(app, ["install", "uninstall", "--data-dir", str(d)]).output
    )
    assert blocked["receipt"]["lifecycle"]["state"] == "blocked"
    out = json.loads(
        runner.invoke(
            app, ["install", "uninstall", "--data-dir", str(d), "--yes"]
        ).output
    )
    assert out["receipt"]["operation"] == "uninstall"
    assert out["receipt"]["lifecycle"]["state"] == "verified"


def test_status_doc_lifecycle(tmp_path: Path, isolated_home: Path) -> None:
    d = tmp_path / ".forge-lens"
    assert _install(d).exit_code == 0
    out = json.loads(
        runner.invoke(app, ["install", "status", "--data-dir", str(d)]).output
    )
    lc = out["lifecycle"]
    assert lc["schema"] == "forge/InstallLifecycle/v1"
    assert lc["state"] == "verified"  # receipt-verified state on disk
    # after uninstall the same doc degrades honestly
    runner.invoke(app, ["install", "uninstall", "--data-dir", str(d), "--yes"])
    out = json.loads(
        runner.invoke(app, ["install", "status", "--data-dir", str(d)]).output
    )
    assert out["state"] == "uninstalled"
    assert out["lifecycle"]["state"] == "unverified"


def test_doctor_lifecycle_states(tmp_path: Path, isolated_home: Path) -> None:
    d = tmp_path / ".forge-lens"
    # nothing installed — unverified, not a fake failure claim
    out = json.loads(
        runner.invoke(app, ["install", "doctor", "--data-dir", str(d)]).output
    )
    assert out["lifecycle"]["state"] == "unverified"
    # healthy install — ready
    assert _install(d).exit_code == 0
    out = json.loads(
        runner.invoke(app, ["install", "doctor", "--data-dir", str(d)]).output
    )
    assert out["lifecycle"]["state"] == "ready"


def test_receipt_state_matches_closed_vocabulary() -> None:
    """The derivation is a pure function of the doc — same rules the
    orchestrator applies to child receipts it did not write."""
    assert lifecycle.receipt_state({"status": "planned"})[0] == "planned"
    assert lifecycle.receipt_state({"status": "blocked"})[0] == "blocked"
    assert (
        lifecycle.receipt_state({"status": "completed", "verification": {"status": "PASS"}})[0]
        == "verified"
    )
    assert (
        lifecycle.receipt_state({"status": "completed", "verification": {"status": "FAIL"}})[0]
        == "failed"
    )
    assert lifecycle.receipt_state({"status": "completed"})[0] == "applied"
    assert lifecycle.receipt_state({"status": "failed"})[0] == "failed"
    assert (
        lifecycle.receipt_state(
            {"status": "failed", "checks": [{"status": "BLOCKED"}]}
        )[0]
        == "blocked"
    )
    assert lifecycle.receipt_state({})[0] == "unverified"
    assert lifecycle.receipt_state({"status": "bogus"})[0] == "unverified"


# --------------------------------------------------------------------------
# incremental reinstall — sha256 hash-compare (§7.4)
# --------------------------------------------------------------------------


def test_first_install_delta_and_managed_hashes(
    tmp_path: Path, isolated_home: Path
) -> None:
    d = tmp_path / ".forge-lens"
    (d).mkdir(parents=True)
    tracked = d / "notes.txt"
    tracked.write_text("managed content", encoding="utf-8")
    result = _install(d, "--file", "notes.txt")
    receipt = json.loads(result.output)["receipt"]
    assert receipt["managed_files"] == ["config.toml", "notes.txt"]
    hashes = receipt["managed_hashes"]
    assert set(hashes) == {"config.toml", "notes.txt"}
    assert hashes["notes.txt"] == (
        f"sha256:{hashlib.sha256(tracked.read_bytes()).hexdigest()}"
    )
    delta = receipt["reinstall"]
    assert delta["baseline"] == "first_install"
    assert sorted(delta["added"]) == ["config.toml", "notes.txt"]
    assert delta["user_state_preserved"] is True


def test_reinstall_reports_unchanged_and_preserves_bytes(
    tmp_path: Path, isolated_home: Path
) -> None:
    """Second install: nothing changed → every managed file is
    hash-unchanged and config.toml keeps its exact bytes."""
    d = tmp_path / ".forge-lens"
    assert _install(d, "--file", "notes.txt").exit_code == 0
    cfg_before = (d / "config.toml").read_bytes()
    result = _install(d, "--file", "notes.txt")
    assert result.exit_code == 0
    delta = json.loads(result.output)["receipt"]["reinstall"]
    assert delta["baseline"] == "prior_receipt"
    assert delta["hash_compare"] == "sha256"
    # notes.txt declared but absent → missing; config.toml identical
    assert delta["unchanged"] == ["config.toml"]
    assert delta["changed"] == []
    assert delta["missing"] == ["notes.txt"]
    assert (d / "config.toml").read_bytes() == cfg_before


def test_reinstall_detects_drift_and_manifest_changes(
    tmp_path: Path, isolated_home: Path
) -> None:
    d = tmp_path / ".forge-lens"
    d.mkdir(parents=True)
    (d / "a.txt").write_text("v1", encoding="utf-8")
    (d / "b.txt").write_text("keep", encoding="utf-8")
    assert _install(d, "--file", "a.txt", "--file", "b.txt").exit_code == 0
    # user edits a managed file + drops one from the manifest
    (d / "a.txt").write_text("v2-drifted", encoding="utf-8")
    result = _install(d, "--file", "a.txt")
    delta = json.loads(result.output)["receipt"]["reinstall"]
    assert delta["changed"] == ["a.txt"]  # hash drift surfaced, not hidden
    assert delta["removed_from_manifest"] == ["b.txt"]
    # and the drifted bytes survive — install never rewrites managed files
    assert (d / "a.txt").read_text(encoding="utf-8") == "v2-drifted"


def test_reinstall_against_legacy_receipt_is_honest(
    tmp_path: Path, isolated_home: Path
) -> None:
    """A pre-wave-10 receipt has no ``managed_hashes`` — the delta must
    report ``hash_compare: unavailable`` instead of claiming
    unchanged-by-absence."""
    d = tmp_path / ".forge-lens"
    (d / "install" / "receipts").mkdir(parents=True)
    (d / "config.toml").write_text("[lens]\n", encoding="utf-8")
    legacy = {
        "schema": "forge/InstallReceipt/v1",
        "receipt_id": "sha256:" + "0" * 64,
        "forge_id": "forge-lens",
        "operation": "install",
        "status": "completed",
        "managed_files": ["config.toml"],
        "verification": {"status": "PASS"},
        "created_at": "2026-01-01T00:00:00Z",
        "ts_ns": 1,
    }
    (d / "install" / "receipt.json").write_text(json.dumps(legacy), encoding="utf-8")
    result = _install(d)
    assert result.exit_code == 0
    delta = json.loads(result.output)["receipt"]["reinstall"]
    assert delta["baseline"] == "prior_receipt_no_hashes"
    assert delta["hash_compare"] == "unavailable"
    assert delta["unchanged"] == []  # no fake "same bytes" claim


def test_managed_hash_never_escapes_data_dir(
    tmp_path: Path, isolated_home: Path
) -> None:
    """``--file ../outside`` resolves outside the data dir → recorded in
    managed_files, never hashed (same containment as uninstall)."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    d = tmp_path / ".forge-lens"
    result = _install(d, "--file", "../outside.txt")
    assert result.exit_code == 0
    receipt = json.loads(result.output)["receipt"]
    assert "../outside.txt" in receipt["managed_files"]
    assert "../outside.txt" not in receipt["managed_hashes"]
    assert "../outside.txt" in receipt["reinstall"]["missing"]


def test_dry_run_previews_delta_without_writes(
    tmp_path: Path, isolated_home: Path
) -> None:
    d = tmp_path / ".forge-lens"
    assert _install(d).exit_code == 0
    out = json.loads(
        runner.invoke(app, ["install", "--data-dir", str(d), "--dry-run"]).output
    )
    delta = out["receipt"]["reinstall"]
    assert delta["baseline"] == "prior_receipt"
    assert delta["unchanged"] == ["config.toml"]


# --------------------------------------------------------------------------
# context metadata honest + host surfaces sync (§8.1-8.2, §9)
# --------------------------------------------------------------------------


def _manifest() -> dict:
    return json.loads((REPO / "forge.agentic.json").read_text(encoding="utf-8"))


def _frontmatter_description(path: Path) -> str | None:
    """The ``description:`` line inside the leading ``---`` block — the
    single source of truth the manifest mirrors."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        if line.startswith("description:"):
            return line.split(":", 1)[1].strip()
    return None


def test_agentic_resources_metadata_matches_host_files() -> None:
    """Every declared skill/agent resource resolves on disk AND mirrors
    the frontmatter description verbatim — L1 metadata the host consumes
    must be provably in sync with the asset it points to."""
    doc = _manifest()
    for res in doc["resources"]:
        if res["kind"] not in ("skill", "agent"):
            continue
        loc = res.get("location")
        assert loc, f"{res['id']}: available resource without location"
        path = REPO / loc
        assert path.is_file(), f"{res['id']}: missing {loc}"
        front = _frontmatter_description(path)
        assert front is not None, f"{loc}: no frontmatter description"
        assert res["description"] == front, f"{res['id']}: manifest/frontmatter drift"
        assert res["domain"] == "observability"
        assert res["tags"]
        assert all(re.fullmatch(r"[a-z0-9-]+", t) for t in res["tags"])


def test_agentic_manifest_surfaces_are_real() -> None:
    """Declared surfaces resolve to real implementations — never a
    promise of capability that isn't there."""
    doc = _manifest()
    # mcp_verify_tool must be a tool the MCP server actually serves
    from forge_lens import mcp_server

    tools = {t["name"] for t in mcp_server._tools()}
    assert doc["mcp_verify_tool"] in tools
    # host adapters named in the manifest have real adapter modules
    adapter_modules = {p.stem for p in (REPO / "src/forge_lens/adapters").glob("*.py")}
    for host in doc["host_adapters"]:
        assert (
            f"{host}_code" in adapter_modules or host in adapter_modules
        ), f"host adapter {host!r} declared without an adapter module"
    # workflow verbs are real CLI commands
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for wf in doc["workflows"]:
        argv = [a for a in wf["command"] if not a.startswith("{")]
        assert argv[0] in result.output, f"workflow {wf['id']}: verb {argv[0]} not on CLI"


def test_minimal_l0_boot_stays_bounded() -> None:
    """AGENTS.md is the repo's L0 boot doc (§8.1): compact enough to sit
    in permanent context, with the commands + hard rules pointers a host
    needs before any retrieval."""
    agents = REPO / "AGENTS.md"
    raw = agents.read_bytes()
    assert len(raw) < 6144, f"AGENTS.md grew to {len(raw)}B — L0 must stay minimal"
    text = raw.decode("utf-8")
    for required in ("## Commands", "## Hard rules", "pytest", "forge-lens doctor"):
        assert required in text


def test_agentic_and_packaged_manifests_identical() -> None:
    assert (REPO / "forge.agentic.json").read_bytes() == (
        REPO / "src" / "forge_lens" / "forge.agentic.json"
    ).read_bytes()
