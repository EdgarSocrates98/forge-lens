"""Regression coverage for ``scripts/forge_bootstrap.py``.

The bootstrap is a security-sensitive installer: it builds argv for the
interpreter selection, invokes pip with possibly credential-carrying
``--dist`` specs, classifies the checkout's provenance, persists shell-rc
state and writes the registry manifest + install receipt. These tests drive
the pure seams directly — the module is loaded from ``scripts/`` via
``importlib`` (the directory is not a package), ``_run``/``shutil.which``
are replaced by fakes, and ``FORGE_HOME_OVERRIDE`` plus a patched
``os.environ`` keep every write inside ``tmp_path``. No real install,
venv, pip, or network occurs.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "forge_bootstrap.py"


def _load_module() -> Any:
    spec = importlib.util.spec_from_file_location("forge_bootstrap", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fb() -> Any:
    """A freshly loaded forge_bootstrap module (stateless module; the env
    reads in ``_home``/``_posix_env_paths`` happen at call time)."""
    return _load_module()


@pytest.fixture
def forge_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate every installer write under ``tmp_path``.

    ``FORGE_HOME_OVERRIDE`` relocates ``~/.forge`` and ``~/.local/bin``;
    ``HOME``/``XDG_CONFIG_HOME`` relocate the shell-rc targets; ``os.environ``
    itself is replaced by a plain dict so persistence code that mutates the
    process environment (exported vars, PATH prepend) cannot leak into other
    tests.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setenv("FORGE_HOME_OVERRIDE", str(home))
    monkeypatch.setenv("HOME", str(home))
    # Path.home() honors USERPROFILE (not HOME) on Windows — without this
    # _posix_env_paths would leak the rc-file write into the real profile.
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    monkeypatch.setenv("SHELL", "/bin/bash")
    for var in ("FORGE_PYTHON", "MSYSTEM", "OSTYPE"):
        monkeypatch.delenv(var, raising=False)
    return home


def _write_forge_json(repo: Path, **overrides: Any) -> Path:
    cfg = {
        "forge_id": "forge-lens",
        "package": "forge_lens",
        "distribution": "forge-lens",
        "cli": "forge-lens",
        "python": ">=3.11",
        "extras": ["server"],
        "mcp_server_name": "forge-lens",
        "mcp_command": ["forge-lens", "mcp-serve"],
    }
    cfg.update(overrides)
    (repo / "forge.json").write_text(json.dumps(cfg), encoding="utf-8")
    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimally Forge-shaped checkout: manifest + expected structure."""
    r = tmp_path / "repo"
    (r / "scripts").mkdir(parents=True)
    (r / "scripts" / "forge_bootstrap.py").write_text("# vendored\n", encoding="utf-8")
    (r / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return _write_forge_json(r)


def _stdout_json(capsys: pytest.CaptureFixture) -> dict:
    """Parse the JSON doc out of stdout — ``_log`` lines may precede it."""
    out = capsys.readouterr().out
    return json.loads(out[out.find("{\n") :])


def _cp(
    cmd: list[str], rc: int = 0, out: str = "", err: str = ""
) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, rc, out, err)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------
# redact_url_userinfo — persisted specs must never carry credentials
# --------------------------------------------------------------------------


def test_redact_url_userinfo_strips_credentials(fb: Any) -> None:
    assert fb.redact_url_userinfo("https://user:token@host/x") == "https://***@host/x"
    assert fb.redact_url_userinfo("http://u:p@h") == "http://***@h"


def test_redact_url_userinfo_leaves_clean_url(fb: Any) -> None:
    assert fb.redact_url_userinfo("https://host/x.whl") == "https://host/x.whl"
    assert fb.redact_url_userinfo("no url at all") == "no url at all"


def test_redact_url_userinfo_multiple_in_one_text(fb: Any) -> None:
    text = "a https://u:p@h1/x b http://u2:p2@h2/y c https://clean/z"
    assert fb.redact_url_userinfo(text) == "a https://***@h1/x b http://***@h2/y c https://clean/z"


def test_redact_url_userinfo_percent_encoded_at(fb: Any) -> None:
    # A percent-encoded '@' inside userinfo stays within [^/@\s]+ → redacted.
    assert fb.redact_url_userinfo("https://u%40x:pw@host/w") == "https://***@host/w"


def test_redact_url_userinfo_raw_at_in_userinfo_redacts_whole(fb: Any) -> None:
    # The userinfo group is `[^/\s]+` (interior '@' allowed): a raw '@'
    # inside userinfo no longer leaves `raw:pw@` visible — the tail up
    # to the last '@' before the path redacts with the rest.
    assert fb.redact_url_userinfo("https://u@raw:pw@host/w") == "https://***@host/w"


def test_redact_url_userinfo_at_in_path_untouched(fb: Any) -> None:
    # '@' after a '/' cannot be userinfo — [^/@\s]+ cannot cross the slash.
    assert fb.redact_url_userinfo("https://host/path@x") == "https://host/path@x"


def test_redact_url_userinfo_preserves_port_and_path(fb: Any) -> None:
    out = fb.redact_url_userinfo("https://u:p@host:8443/wheelhouse/x.whl")
    assert out == "https://***@host:8443/wheelhouse/x.whl"


def test_redact_url_userinfo_git_scheme_mid_string(fb: Any) -> None:
    # The regex is not anchored to the string start, so `git+https://`
    # userinfo is still stripped — `git+` survives as prefix text.
    assert fb.redact_url_userinfo("git+https://u:p@h/x") == "git+https://***@h/x"


def test_redact_url_userinfo_non_http_scheme_untouched(fb: Any) -> None:
    assert fb.redact_url_userinfo("ssh://u:p@h/x") == "ssh://u:p@h/x"


# --------------------------------------------------------------------------
# canonical_dist_name / split_dist_spec / compose_dist_req (PEP 503)
# --------------------------------------------------------------------------


def test_canonical_dist_name_normalizes_pep503(fb: Any) -> None:
    assert fb.canonical_dist_name("Forge_Lens") == "forge-lens"
    assert fb.canonical_dist_name("FORGE-LENS") == "forge-lens"


def test_canonical_dist_name_equivalent_spellings(fb: Any) -> None:
    for spelling in ("sparkforge_aws", "SparkForge-AWS", "sparkforge.aws", " sparkforge--aws "):
        assert fb.canonical_dist_name(spelling) == "sparkforge-aws"


def test_split_dist_spec_basic_forms(fb: Any) -> None:
    assert fb.split_dist_spec("forge-lens") == ("forge-lens", "")
    assert fb.split_dist_spec("forge-lens[server]==0.1.0") == (
        "forge-lens",
        "[server]==0.1.0",
    )
    assert fb.split_dist_spec("forge-lens==0.1.0") == ("forge-lens", "==0.1.0")
    assert fb.split_dist_spec("forge-lens @ https://h/x") == (
        "forge-lens",
        " @ https://h/x",
    )
    assert fb.split_dist_spec("  padded  ") == ("padded", "")


def test_split_dist_spec_traversal_string_is_not_rejected_here(fb: Any) -> None:
    # `..` matches the name class — split alone does NOT stop traversal; the
    # canonical-name gate in run() is the actual guard (tested below).
    assert fb.split_dist_spec("../evil") == ("..", "/evil")


def test_split_dist_spec_rejects_specs_without_name(fb: Any) -> None:
    for bad in ("==1.0", "[e]==v", "@ https://h/x", "   "):
        with pytest.raises(fb.Refusal) as exc:
            fb.split_dist_spec(bad)
        assert exc.value.kind == "FORGE-INSTALL-DIST-INVALID"


def test_compose_dist_req_inserts_extras_before_specifier(fb: Any) -> None:
    # `name==v[e]` is a specifier that matches nothing — extras go first.
    assert fb.compose_dist_req("name==v", ["e"]) == "name[e]==v"
    assert fb.compose_dist_req("name", ["e"]) == "name[e]"


def test_compose_dist_req_merges_declared_extras(fb: Any) -> None:
    assert fb.compose_dist_req("name[e2]==1.0", ["e1", "e2"]) == "name[e1,e2]==1.0"
    assert fb.compose_dist_req("name[b,a]==v", []) == "name[a,b]==v"  # sorted + deduped


def test_compose_dist_req_no_extras_passthrough(fb: Any) -> None:
    assert fb.compose_dist_req("name==1.0", []) == "name==1.0"
    assert fb.compose_dist_req("name", []) == "name"


def test_compose_dist_req_url_spec_gets_extras_before_tail(fb: Any) -> None:
    spec = "forge-lens @ https://wheelhouse/fl-1.0-py3-none-any.whl"
    out = fb.compose_dist_req(spec, ["server"])
    assert out == "forge-lens[server] @ https://wheelhouse/fl-1.0-py3-none-any.whl"


def test_dist_spec_round_trip_keeps_name(fb: Any) -> None:
    for spec in ("Forge_Lens[server]==0.1.0", "forge-lens", "forge.lens>=1"):
        composed = fb.compose_dist_req(spec, ["x"])
        assert fb.split_dist_spec(composed)[0] == fb.split_dist_spec(spec)[0]
        assert fb.canonical_dist_name(fb.split_dist_spec(composed)[0]) == "forge-lens"


# --------------------------------------------------------------------------
# run() --dist gates: PEP 503 name match + --allow-url
# --------------------------------------------------------------------------


def test_run_dist_refuses_wrong_distribution_name(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--dist", "other-dist==1.0", "--quiet"])
    assert rc == 2
    doc = _stdout_json(capsys)
    assert doc["error"]["kind"] == "FORGE-INSTALL-DIST-MISMATCH"


def test_run_dist_refuses_path_traversal_name(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    # `split_dist_spec` yields name `..`; canonical `..` → `-` ≠ forge-lens.
    for spec in ("../evil", "..\\evil", "./sneaky", "evil/pkg"):
        rc = fb.run(["--repo", str(repo), "--dist", spec, "--quiet"])
        assert rc == 2, spec
        assert _stdout_json(capsys)["error"]["kind"] == "FORGE-INSTALL-DIST-MISMATCH"


def test_run_dist_accepts_canonical_match_dry_run(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--dist", "forge-lens==0.1.0", "--dry-run", "--quiet"])
    doc = _stdout_json(capsys)
    assert rc == 0
    assert doc["status"] == "planned"
    assert doc["install_method"] == "pypi-wheel"
    assert {s["stage"] for s in doc["stages"]} >= {"resolve-python", "smoke", "register"}
    assert all(s["status"] == "planned" for s in doc["stages"])


def test_run_dist_accepts_pep503_name_variant(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    for spec in ("Forge_Lens==0.1.0", "FORGE-LENS", "forge__lens>=0.1"):
        rc = fb.run(["--repo", str(repo), "--dist", spec, "--dry-run", "--quiet"])
        assert rc == 0, spec
        _stdout_json(capsys)


def test_run_dist_empty_spec_refused(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--dist", "   ", "--quiet"])
    assert rc == 2
    assert _stdout_json(capsys)["error"]["kind"] == "FORGE-INSTALL-DIST-INVALID"


def test_run_dist_editable_conflict(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--dist", "forge-lens", "--editable", "--quiet"])
    assert rc == 2
    assert _stdout_json(capsys)["error"]["kind"] == "FORGE-INSTALL-MODE-CONFLICT"


def test_run_dist_unparseable_spec_refused(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--dist", "==0.1.0", "--quiet"])
    assert rc == 2
    assert _stdout_json(capsys)["error"]["kind"] == "FORGE-INSTALL-DIST-INVALID"


def test_run_dist_url_refused_without_allow_url(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    spec = "forge-lens @ https://wheelhouse/fl-1.0-py3-none-any.whl"
    rc = fb.run(["--repo", str(repo), "--dist", spec, "--quiet"])
    assert rc == 2
    doc = _stdout_json(capsys)
    assert doc["error"]["kind"] == "FORGE-INSTALL-DIST-URL"


def test_run_dist_url_refusal_detail_is_redacted(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    spec = "forge-lens @ https://u:p@wheelhouse/fl.whl"
    rc = fb.run(["--repo", str(repo), "--dist", spec, "--quiet"])
    assert rc == 2
    detail = _stdout_json(capsys)["error"]["detail"]
    assert "u:p@" not in detail
    assert "***@" in detail


def test_run_dist_url_wrong_name_still_refused_with_flag(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    # --allow-url lifts the URL refusal, never the distribution-name gate.
    spec = "evil-dist @ https://wheelhouse/evil-1.0-py3-none-any.whl"
    rc = fb.run(["--repo", str(repo), "--dist", spec, "--allow-url", "--quiet"])
    assert rc == 2
    assert _stdout_json(capsys)["error"]["kind"] == "FORGE-INSTALL-DIST-MISMATCH"


def test_run_dist_url_accepted_with_allow_url(
    fb: Any, repo: Path, forge_home: Path, capsys: pytest.CaptureFixture
) -> None:
    spec = "forge-lens @ https://wheelhouse/fl-1.0-py3-none-any.whl"
    rc = fb.run(["--repo", str(repo), "--dist", spec, "--allow-url", "--dry-run", "--quiet"])
    assert rc == 0
    assert _stdout_json(capsys)["status"] == "planned"


# --------------------------------------------------------------------------
# install_package — pip argv construction + refusal redaction
# --------------------------------------------------------------------------


def test_install_package_dist_sends_verbatim_spec_to_pip(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pip needs the real userinfo to fetch the wheel — only the persisted
    spec is redacted. Assert the in-memory requirement reaches pip intact."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        fb, "_run", lambda cmd, timeout=300, **kw: calls.append(cmd) or _cp(cmd)
    )
    venv = tmp_path / "v"
    spec = "forge-lens @ https://u:p@wheelhouse/fl-1.0-py3-none-any.whl"
    fb.install_package(tmp_path, venv, ["server"], False, False, lambda m: None, dist=spec)
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:3] == [str(fb._venv_python(venv)), "-m", "pip"]
    assert cmd[3:] == [
        "install",
        "--upgrade",
        "forge-lens[server] @ https://u:p@wheelhouse/fl-1.0-py3-none-any.whl",
    ]


def test_install_package_dist_failure_redacts_req_and_stderr(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        fb,
        "_run",
        lambda cmd, timeout=300, **kw: _cp(cmd, 1, "", "403 https://u:p@wheelhouse/x denied"),
    )
    spec = "forge-lens @ https://u:p@wheelhouse/fl.whl"
    with pytest.raises(fb.Refusal) as exc:
        fb.install_package(tmp_path, tmp_path / "v", [], False, False, lambda m: None, dist=spec)
    assert exc.value.kind == "FORGE-INSTALL-RUNTIME-FAILED"
    assert "u:p@" not in exc.value.detail
    assert "***@" in exc.value.detail


def test_install_package_dist_log_line_is_redacted(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd))
    logged: list[str] = []
    spec = "forge-lens @ https://u:p@wheelhouse/fl.whl"
    fb.install_package(tmp_path, tmp_path / "v", [], False, False, logged.append, dist=spec)
    assert any("***@" in line for line in logged)
    assert all("u:p@" not in line for line in logged)


def test_install_package_editable_uses_e_flag(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        fb, "_run", lambda cmd, timeout=300, **kw: calls.append(cmd) or _cp(cmd)
    )
    repo, venv = tmp_path / "repo", tmp_path / "v"
    fb.install_package(repo, venv, ["server"], True, False, lambda m: None)
    assert len(calls) == 1
    assert "-e" in calls[0]
    assert calls[0][-1] == f"{repo}[server]"


def test_install_package_wheel_build_then_install(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    install_root = tmp_path / "root"
    venv = install_root / ".venv"
    whl_dir = install_root / ".wheels"

    def fake_run(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        if "wheel" in cmd:
            whl_dir.mkdir(parents=True, exist_ok=True)
            (whl_dir / "fl-0.1.0-py3-none-any.whl").write_bytes(b"whl")
        return _cp(cmd)

    monkeypatch.setattr(fb, "_run", fake_run)
    repo = tmp_path / "repo"
    fb.install_package(repo, venv, ["server"], False, False, lambda m: None)
    assert calls[0][3] == "wheel"
    assert calls[1][-1] == f"{whl_dir / 'fl-0.1.0-py3-none-any.whl'}[server]"
    assert not whl_dir.exists()  # .wheels scratch dir is cleaned up


def test_install_package_wheel_build_failure_falls_back_to_checkout(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        # `pip wheel` fails (build backend unavailable); `pip install` succeeds.
        return _cp(cmd, 1 if "wheel" in cmd else 0)

    monkeypatch.setattr(fb, "_run", fake_run)
    repo, venv = tmp_path / "repo", tmp_path / "root" / ".venv"
    fb.install_package(repo, venv, [], False, False, lambda m: None)
    # wheel build unavailable → target degrades to the checkout path itself.
    assert calls[1][-1] == str(repo)


def test_install_package_failure_offline_hint(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 1, "", "boom"))
    repo, venv = tmp_path / "repo", tmp_path / "v"
    with pytest.raises(fb.Refusal) as exc:
        fb.install_package(repo, venv, [], True, False, lambda m: None)
    assert "--allow-download" in exc.value.detail
    with pytest.raises(fb.Refusal) as exc2:
        fb.install_package(repo, venv, [], True, True, lambda m: None)
    assert "--allow-download" not in exc2.value.detail


# --------------------------------------------------------------------------
# _checksum_integrity + _source_provenance (trust classes A/B/C/none)
# --------------------------------------------------------------------------


def test_checksum_integrity_unverified_without_manifest(fb: Any, repo: Path) -> None:
    assert fb._checksum_integrity(repo) == "unverified"


def test_checksum_integrity_verified_sha256sums(fb: Any, repo: Path) -> None:
    target = repo / "payload.bin"
    target.write_bytes(b"payload")
    (repo / "SHA256SUMS").write_text(f"{_sha256(target)}  payload.bin\n", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "verified"


def test_checksum_integrity_binary_marker_and_uppercase_digest(fb: Any, repo: Path) -> None:
    target = repo / "a.bin"
    target.write_bytes(b"aaa")
    (repo / "SHA256SUMS").write_text(
        f"{_sha256(target).upper()} *a.bin\n", encoding="utf-8"
    )
    assert fb._checksum_integrity(repo) == "verified"


def test_checksum_integrity_alternate_filename(fb: Any, repo: Path) -> None:
    target = repo / "a.bin"
    target.write_bytes(b"aaa")
    (repo / "checksums.sha256").write_text(
        f"{_sha256(target)}  a.bin\n", encoding="utf-8"
    )
    assert fb._checksum_integrity(repo) == "verified"


def test_checksum_integrity_diverged_entry_is_partial(fb: Any, repo: Path) -> None:
    (repo / "a.bin").write_bytes(b"aaa")
    (repo / "SHA256SUMS").write_text(f"{'0' * 64}  a.bin\n", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"


def test_checksum_integrity_missing_listed_file_is_partial(fb: Any, repo: Path) -> None:
    target = repo / "a.bin"
    target.write_bytes(b"aaa")
    (repo / "SHA256SUMS").write_text(f"{_sha256(target)}  ghost.bin\n", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"


def test_checksum_integrity_path_escape_is_partial(fb: Any, repo: Path) -> None:
    """A listed entry escaping the source root is never trusted — even one
    escape degrades the whole manifest to 'partial'."""
    outside = repo.parent / "outside.bin"
    outside.write_bytes(b"out")
    lines = f"{_sha256(outside)}  ../outside.bin\n"
    (repo / "SHA256SUMS").write_text(lines, encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"


def test_checksum_integrity_absolute_path_entry_is_partial(fb: Any, repo: Path) -> None:
    (repo / "SHA256SUMS").write_text(f"{'a' * 64}  /etc/passwd\n", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"


def test_checksum_integrity_json_files_list(fb: Any, repo: Path) -> None:
    target = repo / "a.bin"
    target.write_bytes(b"aaa")
    (repo / ".forge-checksums.json").write_text(
        json.dumps({"files": [{"path": "a.bin", "sha256": _sha256(target)}]}),
        encoding="utf-8",
    )
    assert fb._checksum_integrity(repo) == "verified"


def test_checksum_integrity_json_flat_map(fb: Any, repo: Path) -> None:
    target = repo / "a.bin"
    target.write_bytes(b"aaa")
    (repo / ".forge-checksums.json").write_text(
        json.dumps({"a.bin": _sha256(target)}), encoding="utf-8"
    )
    assert fb._checksum_integrity(repo) == "verified"


def test_checksum_integrity_json_files_not_list_is_partial(fb: Any, repo: Path) -> None:
    (repo / ".forge-checksums.json").write_text(
        json.dumps({"files": "a.bin"}), encoding="utf-8"
    )
    assert fb._checksum_integrity(repo) == "partial"


def test_checksum_integrity_malformed_files_are_partial(fb: Any, repo: Path) -> None:
    (repo / ".forge-checksums.json").write_text("{not json", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"
    (repo / ".forge-checksums.json").unlink()
    (repo / "SHA256SUMS").write_text("garbage without digests\n", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"
    (repo / "SHA256SUMS").write_text("", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"


def test_checksum_integrity_entry_cap_is_partial(fb: Any, repo: Path) -> None:
    """More than _CHECKSUM_MAX_ENTRIES entries → 'partial' before any file
    I/O — an unbounded manifest cannot stall the installer."""
    over = fb._CHECKSUM_MAX_ENTRIES + 1
    lines = "".join(f"{'a' * 64}  f{i}\n" for i in range(over))
    (repo / "SHA256SUMS").write_text(lines, encoding="utf-8")
    assert fb._checksum_integrity(repo) == "partial"


def test_checksum_integrity_first_manifest_wins(fb: Any, repo: Path) -> None:
    target = repo / "a.bin"
    target.write_bytes(b"aaa")
    (repo / "SHA256SUMS").write_text(f"{_sha256(target)}  a.bin\n", encoding="utf-8")
    (repo / ".forge-checksums.json").write_text("{broken", encoding="utf-8")
    assert fb._checksum_integrity(repo) == "verified"


def _mk_forge_repo(path: Path, *, git: str | None = None, checksums: bool = False) -> Path:
    """Forge-shaped checkout for provenance tests (no forge.json — the
    manifest_valid flag is passed explicitly, matching run() usage)."""
    (path / "scripts").mkdir(parents=True, exist_ok=True)
    (path / "scripts" / "forge_bootstrap.py").write_text("# vendored\n", encoding="utf-8")
    (path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    if git == "dir":
        (path / ".git").mkdir()
    elif git == "file":
        (path / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    if checksums:
        payload = path / "pyproject.toml"
        (path / "SHA256SUMS").write_text(
            f"{_sha256(payload)}  pyproject.toml\n", encoding="utf-8"
        )
    return path


def test_source_provenance_none_without_manifest(fb: Any, tmp_path: Path) -> None:
    repo = _mk_forge_repo(tmp_path / "r", git="dir", checksums=True)
    doc = fb._source_provenance(repo, manifest_valid=False)
    assert doc["source_trust"] == "none"
    assert doc["is_git"] is True
    assert doc["manifest_valid"] is False


def test_source_provenance_trust_c_manifest_only(fb: Any, tmp_path: Path) -> None:
    bare = tmp_path / "r"
    bare.mkdir()
    doc = fb._source_provenance(bare, manifest_valid=True)
    assert doc["source_trust"] == "C"


def test_source_provenance_structure_requires_both_halves(fb: Any, tmp_path: Path) -> None:
    half = tmp_path / "r"
    (half / "scripts").mkdir(parents=True)
    (half / "scripts" / "forge_bootstrap.py").write_text("x", encoding="utf-8")
    # no packaging marker → structure_ok False → C, not B
    assert fb._source_provenance(half, manifest_valid=True)["source_trust"] == "C"


def test_source_provenance_trust_b_no_git(fb: Any, tmp_path: Path) -> None:
    repo = _mk_forge_repo(tmp_path / "r")
    doc = fb._source_provenance(repo, manifest_valid=True)
    assert doc["source_trust"] == "B"
    assert doc["is_git"] is False


def test_source_provenance_git_without_checksums_stays_b(fb: Any, tmp_path: Path) -> None:
    repo = _mk_forge_repo(tmp_path / "r", git="dir")
    doc = fb._source_provenance(repo, manifest_valid=True)
    assert doc["source_trust"] == "B"
    assert doc["is_git"] is True
    assert doc["integrity"] == "unverified"


def test_source_provenance_trust_a(fb: Any, tmp_path: Path) -> None:
    repo = _mk_forge_repo(tmp_path / "r", git="dir", checksums=True)
    doc = fb._source_provenance(repo, manifest_valid=True)
    assert doc == {
        "is_git": True,
        "source_trust": "A",
        "manifest_valid": True,
        "integrity": "verified",
    }


def test_source_provenance_gitfile_worktree_is_not_trust_a(fb: Any, tmp_path: Path) -> None:
    # A linked worktree carries `.git` as a FILE — `is_git` observes it, but
    # `is_repo` (dir) stays False so A is unreachable.
    repo = _mk_forge_repo(tmp_path / "r", git="file", checksums=True)
    doc = fb._source_provenance(repo, manifest_valid=True)
    assert doc["is_git"] is True
    assert doc["source_trust"] == "B"


# --------------------------------------------------------------------------
# managed_files — the receipt's owned-artifact set
# --------------------------------------------------------------------------


def test_managed_files_exact_owned_set(fb: Any, forge_home: Path) -> None:
    cfg = {"forge_id": "forge-lens", "cli": "forge-lens"}
    root = Path("/install/root")
    files = fb.managed_files(cfg, root, persist_env=False, no_launcher=False)
    expected = {
        str(root),
        str(forge_home / ".forge" / "installations" / "forge-lens.json"),
        str(forge_home / ".forge" / "installations" / ".forge-lens-bootstrap.json"),
        str(forge_home / ".local" / "bin" / "forge-lens"),
    }
    if os.name == "nt":
        # write_launcher ships both a .cmd and an sh shim on Windows.
        expected.add(str(forge_home / ".local" / "bin" / "forge-lens.cmd"))
    assert files == sorted(expected)


def test_managed_files_no_launcher_drops_shim(fb: Any, forge_home: Path) -> None:
    cfg = {"forge_id": "forge-lens", "cli": "forge-lens"}
    files = fb.managed_files(cfg, Path("/r"), persist_env=False, no_launcher=True)
    assert not any(".local" in f for f in files)


def test_managed_files_persist_env_adds_env_file(fb: Any, forge_home: Path) -> None:
    cfg = {"forge_id": "forge-lens", "cli": "forge-lens"}
    files = fb.managed_files(cfg, Path("/r"), persist_env=True, no_launcher=False)
    env_file = str(forge_home / "xdg" / "forjas" / "env.sh")
    if os.name == "nt" and not fb._is_git_bash():
        # Native Windows persists via HKCU\Environment — no shell env
        # file is owned; the env.sh entry appears only under Git Bash.
        assert env_file not in files
    else:
        assert env_file in files


def test_managed_files_never_lists_shell_rc(fb: Any, forge_home: Path) -> None:
    """The rc-file block is a *store mutation*, not an owned file — an
    uninstall driven by this list must leave ~/.bashrc alone."""
    cfg = {"forge_id": "forge-lens", "cli": "forge-lens"}
    files = fb.managed_files(cfg, Path("/r"), persist_env=True, no_launcher=False)
    assert not any(f.endswith((".bashrc", ".zshrc", ".profile")) for f in files)


def test_managed_files_covers_register_and_receipt_paths(
    fb: Any, forge_home: Path
) -> None:
    """The paths register()/write_receipt() actually write must be inside
    the managed set — otherwise uninstall cannot see them."""
    cfg = {"forge_id": "forge-lens", "cli": "forge-lens"}
    managed = set(fb.managed_files(cfg, Path("/r"), persist_env=False, no_launcher=False))
    assert str(fb.forge_home() / "installations" / "forge-lens.json") in managed
    assert str(fb.forge_home() / "installations" / ".forge-lens-bootstrap.json") in managed


# --------------------------------------------------------------------------
# python selection: _explicit_python_argv / find_python / FORGE_PYTHON
# --------------------------------------------------------------------------


def test_explicit_python_argv_returns_trimmed_path(fb: Any) -> None:
    """Regression: `--python " /usr/bin/python3 "` must yield the TRIMMED
    exe — a padded argv[0] is unexecutable (reviewer finding, fixed at
    ``_explicit_python_argv`` → ``return [trimmed]``)."""
    assert fb._explicit_python_argv(" /usr/bin/python3 ") == ["/usr/bin/python3"]


def test_explicit_python_argv_splits_invocation(fb: Any) -> None:
    assert fb._explicit_python_argv("py -3.11") == ["py", "-3.11"]
    assert fb._explicit_python_argv("  py  -3.11  ") == ["py", "-3.11"]


def test_explicit_python_argv_existing_path_with_spaces_not_split(
    fb: Any, tmp_path: Path
) -> None:
    exe = tmp_path / "my python3"
    exe.write_text("x", encoding="utf-8")
    assert fb._explicit_python_argv(str(exe)) == [str(exe)]


def test_explicit_python_argv_nonexistent_multiword_splits(fb: Any) -> None:
    assert fb._explicit_python_argv("/no/such/py --flag") == ["/no/such/py", "--flag"]


def test_semver_tuple_and_satisfies(fb: Any) -> None:
    assert fb._semver_tuple("3.11") == (3, 11, 0)
    assert fb._semver_tuple("3.11.4") == (3, 11, 4)
    assert fb._semver_tuple("garbage") == (0, 0, 0)
    assert fb._satisfies("3.11", ">=3.11")
    assert fb._satisfies("3.12", ">=3.11,<4")
    assert not fb._satisfies("3.10", ">=3.11")
    assert not fb._satisfies("4.0", ">=3.11,<4")


def test_python_version_argv_results(fb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 0, "3.12.1\n"))
    assert fb._python_version_argv(["py", "-3.12"]) == "3.12.1"
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 1))
    assert fb._python_version_argv(["x"]) is None

    def _boom(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        raise OSError("no such file")

    monkeypatch.setattr(fb, "_run", _boom)
    assert fb._python_version_argv(["x"]) is None

    def _slow(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd, timeout)

    monkeypatch.setattr(fb, "_run", _slow)
    assert fb._python_version_argv(["x"]) is None


def test_resolve_executable(fb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 0, "/real/python3.12\n")
    )
    assert fb._resolve_executable(["py", "-3.12"]) == "/real/python3.12"
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 0, "  \n"))
    assert fb._resolve_executable(["py"]) is None
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 5))
    assert fb._resolve_executable(["py"]) is None


def _version_ok_run(
    version: str = "3.12.4", resolved: str = "/usr/bin/python3.12"
) -> Any:
    """A ``_run`` fake that answers the version + sys.executable probes."""

    def fake_run(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        if "sys.executable" in cmd[-1]:
            return _cp(cmd, 0, resolved + "\n")
        return _cp(cmd, 0, version + "\n")

    return fake_run


def test_find_python_explicit_success(fb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def spy(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        seen.append(cmd)
        return _version_ok_run()(cmd, timeout, **kw)

    monkeypatch.setattr(fb, "_run", spy)
    prov: dict[str, Any] = {}
    out = fb.find_python(">=3.11", "py -3.12", False, lambda m: None, prov)
    assert out == "/usr/bin/python3.12"
    assert seen[0][:2] == ["py", "-3.12"]  # invocation split reached subprocess
    assert prov["source"] == "explicit"
    assert prov["version"] == "3.12.4"


def test_find_python_explicit_incompatible_refused(
    fb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb, "_run", _version_ok_run("3.9.9"))
    with pytest.raises(fb.Refusal) as exc:
        fb.find_python(">=3.11", "/old/python", False, lambda m: None)
    assert exc.value.kind == "FORGE-INSTALL-PYTHON-INCOMPATIBLE"
    assert "3.9.9" in exc.value.detail


def test_find_python_explicit_unusable_refused(
    fb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        raise OSError("missing")

    monkeypatch.setattr(fb, "_run", _boom)
    with pytest.raises(fb.Refusal) as exc:
        fb.find_python(">=3.11", "/ghost/py", False, lambda m: None)
    assert "unusable" in exc.value.detail


def test_find_python_path_scan_hit(fb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    # The scan order differs per OS: `py -3.x` launches on Windows,
    # `python3.x` on POSIX — stub whichever the platform probes first.
    hit = "py" if os.name == "nt" else "python3.12"
    needle = "py -3.9" if os.name == "nt" else "python3.12"
    monkeypatch.setattr(
        fb.shutil, "which", lambda exe: "/usr/bin/" + exe if exe == hit else None
    )
    monkeypatch.setattr(fb, "_run", _version_ok_run("3.12.4", "/resolved/py3.12"))
    prov: dict[str, Any] = {}
    logged: list[str] = []
    out = fb.find_python(">=3.11", None, False, logged.append, prov)
    assert out == "/resolved/py3.12"
    assert prov["source"] == "path"
    assert prov["version"] == "3.12.4"
    assert any(needle in m for m in logged)


def test_find_python_no_candidate_requires_allow_download(
    fb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb.shutil, "which", lambda exe: None)
    with pytest.raises(fb.Refusal) as exc:
        fb.find_python(">=3.11", None, False, lambda m: None)
    assert exc.value.kind == "FORGE-INSTALL-PYTHON-INCOMPATIBLE"
    assert "--allow-download" in exc.value.detail


def test_find_python_uv_fetch_when_allowed(fb: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fb.shutil, "which", lambda exe: "/uv" if exe == "uv" else None
    )

    def fake_run(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        if cmd[:3] == ["uv", "python", "install"]:
            return _cp(cmd)
        if cmd[:3] == ["uv", "python", "find"]:
            return _cp(cmd, 0, "/uv/pythons/3.11/bin/python\n")
        return _cp(cmd, 1)

    monkeypatch.setattr(fb, "_run", fake_run)
    prov: dict[str, Any] = {}
    out = fb.find_python(">=3.11", None, True, lambda m: None, prov)
    assert out == "/uv/pythons/3.11/bin/python"
    assert prov["source"] == "uv"


def test_find_python_uv_install_failure_refused(
    fb: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb.shutil, "which", lambda exe: "/uv" if exe == "uv" else None)
    monkeypatch.setattr(
        fb,
        "_run",
        lambda cmd, timeout=300, **kw: _cp(cmd, 1, "", "offline"),
    )
    with pytest.raises(fb.Refusal) as exc:
        fb.find_python(">=3.11", None, True, lambda m: None)
    assert exc.value.kind == "FORGE-INSTALL-PYTHON-INCOMPATIBLE"
    assert "offline" in exc.value.detail


def test_run_python_source_labels(
    fb: Any,
    repo: Path,
    forge_home: Path,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Receipt `python.source`: 'flag' (--python) beats 'env' (FORGE_PYTHON)
    beats 'path'. Dry-run never spawns the interpreter."""
    rc = fb.run(["--repo", str(repo), "--dry-run", "--quiet"])
    assert rc == 0
    assert _stdout_json(capsys)["python"]["source"] == "path"

    monkeypatch.setenv("FORGE_PYTHON", "/env/python3.12")
    rc = fb.run(["--repo", str(repo), "--dry-run", "--quiet"])
    assert rc == 0
    doc = _stdout_json(capsys)
    assert doc["python"]["source"] == "env"
    assert doc["python"]["selection_reason"] == "FORGE_PYTHON environment variable"

    rc = fb.run(["--repo", str(repo), "--python", "/flag/py", "--dry-run", "--quiet"])
    assert rc == 0
    doc = _stdout_json(capsys)
    assert doc["python"]["source"] == "flag"
    assert doc["python"]["selection_reason"] == "--python flag"


# --------------------------------------------------------------------------
# Pipeline: stage fakes (no real python/venv/pip), real register + receipt
# --------------------------------------------------------------------------


@pytest.fixture
def pipeline(
    fb: Any, repo: Path, forge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Wire the side-effecting stages to fakes so run() executes end-to-end
    without touching the real machine. ``write_launcher``/``register``/
    ``write_receipt`` stay real — they write only under the overridden home.
    """
    install_root = forge_home / ".forge" / "installs" / "forge-lens"
    venv = install_root / ".venv"
    spies: dict[str, Any] = {
        "explicit": None,
        "dist": None,
        "smoke_calls": 0,
        "smoke_result": {"status": "PASS", "version": "0.1.0"},
        "venv": venv,
        "install_root": install_root,
    }

    def fake_find_python(
        spec: str,
        explicit: str | None,
        allow_download: bool,
        log: Any,
        provenance: dict[str, Any] | None = None,
    ) -> str:
        spies["explicit"] = explicit
        if provenance is not None:
            provenance.update(
                {
                    "source": "explicit" if explicit else "path",
                    "version": "3.12.4",
                    "selection_reason": "test seam",
                }
            )
        return "/fake/python3.12"

    def fake_create_venv(root: Path, python: str, log: Any) -> Path:
        bindir = fb._venv_bin(venv)
        bindir.mkdir(parents=True, exist_ok=True)
        fb._venv_python(venv).write_text("x", encoding="utf-8")
        (bindir / "forge-lens").write_text("x", encoding="utf-8")
        return venv

    def fake_install_package(
        repo_: Path,
        venv_: Path,
        extras: list[str],
        editable: bool,
        allow_download: bool,
        log: Any,
        dist: str | None = None,
    ) -> None:
        spies["dist"] = dist
        spies["extras"] = list(extras)
        spies["editable"] = editable

    def fake_smoke(cli: str, venv_: Path, version_cmd: list[str], log: Any) -> dict:
        spies["smoke_calls"] += 1
        return dict(spies["smoke_result"])

    monkeypatch.setattr(fb, "find_python", fake_find_python)
    monkeypatch.setattr(fb, "create_venv", fake_create_venv)
    monkeypatch.setattr(fb, "install_package", fake_install_package)
    monkeypatch.setattr(fb, "smoke", fake_smoke)
    monkeypatch.setattr(fb, "detect_hosts", lambda: {"devin": {"cli": True}})
    return spies


def _manifest(home: Path) -> dict:
    path = home / ".forge" / "installations" / "forge-lens.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _receipt(home: Path) -> dict:
    path = home / ".forge" / "installations" / ".forge-lens-bootstrap.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_pipeline_smoke_runs_before_register(
    fb: Any, repo: Path, forge_home: Path, pipeline: dict, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--quiet"])
    assert rc == 0
    names = [s["stage"] for s in _receipt(forge_home)["stages"]]
    assert names == [
        "resolve-python",
        "create-venv",
        "install-runtime",
        "write-launcher",
        "smoke",
        "register",
    ]
    assert names.index("smoke") < names.index("register")


def test_pipeline_smoke_failure_prevents_registration(
    fb: Any, repo: Path, forge_home: Path, pipeline: dict, capsys: pytest.CaptureFixture
) -> None:
    """A failing smoke must leave NO manifest behind — the receipt still
    records the failure for uninstall/diagnostics."""
    pipeline["smoke_result"] = {"status": "FAIL", "version": ""}
    rc = fb.run(["--repo", str(repo), "--quiet"])
    assert rc == 2
    doc = _stdout_json(capsys)
    assert doc["error"]["kind"] == "FORGE-INSTALL-VERIFY-FAILED"
    assert not (forge_home / ".forge" / "installations" / "forge-lens.json").exists()
    receipt = _receipt(forge_home)
    assert receipt["status"] == "failed"
    assert receipt["error"]["kind"] == "FORGE-INSTALL-VERIFY-FAILED"
    names = [s["stage"] for s in receipt["stages"]]
    assert "smoke" in names
    assert "register" not in names


def test_pipeline_checkout_install_writes_manifest_and_receipt(
    fb: Any, repo: Path, forge_home: Path, pipeline: dict, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--quiet"])
    assert rc == 0
    manifest = _manifest(forge_home)
    assert manifest["schema"] == "forge/InstallationManifest/v1"
    assert manifest["installation_method"] == "wheel"
    assert manifest["source"]["kind"] == "git-checkout"
    assert manifest["source"]["path"] == str(repo)
    assert manifest["mcp"]["command"] == ["forge-lens", "mcp-serve"]
    assert manifest["mcp"]["verified"] is False
    assert manifest["hosts"] == {"devin": {"cli": True}}
    env = manifest["environment"]["variables"]
    assert env["FORGE_LENS_HOME"] == str(repo.resolve())  # checkout kept
    shim = forge_home / ".local" / "bin" / "forge-lens"
    assert shim.exists()  # real write_launcher ran
    receipt = _receipt(forge_home)
    assert receipt["status"] == "completed"
    assert receipt["install_method"] == "wheel"
    assert str(shim) in receipt["managed_files"]
    assert str(forge_home / ".forge" / "installations" / "forge-lens.json") in receipt[
        "managed_files"
    ]


def test_pipeline_dist_install_redacts_credentials_in_manifest(
    fb: Any, repo: Path, forge_home: Path, pipeline: dict, capsys: pytest.CaptureFixture
) -> None:
    spec = "forge-lens @ https://u:p@wheelhouse/fl-1.0-py3-none-any.whl"
    rc = fb.run(["--repo", str(repo), "--dist", spec, "--allow-url", "--quiet"])
    assert rc == 0
    # pip saw the verbatim spec (credentials needed to fetch)…
    assert pipeline["dist"] == spec
    # …but the persisted manifest carries only the redacted form.
    manifest = _manifest(forge_home)
    assert manifest["source"]["kind"] == "pypi"
    assert manifest["source"]["spec"] == (
        "forge-lens[server] @ https://***@wheelhouse/fl-1.0-py3-none-any.whl"
    )
    assert "u:p" not in json.dumps(manifest)
    assert manifest["installation_method"] == "pypi-wheel"
    # A --dist install has no checkout → no <FORGE>_HOME source var.
    env = manifest["environment"]["variables"]
    assert "FORGE_LENS_HOME" not in env
    assert env["FORGE_LENS_VENV"].endswith(".venv")


def test_pipeline_editable_install_method(
    fb: Any, repo: Path, forge_home: Path, pipeline: dict, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--editable", "--quiet"])
    assert rc == 0
    manifest = _manifest(forge_home)
    assert manifest["installation_method"] == "git-checkout"
    assert manifest["source"]["editable"] is True
    assert pipeline["editable"] is True


def test_pipeline_forge_python_env_honored(
    fb: Any,
    repo: Path,
    forge_home: Path,
    pipeline: dict,
    capsys: pytest.CaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FORGE_PYTHON", "/env/python3.12")
    rc = fb.run(["--repo", str(repo), "--quiet"])
    assert rc == 0
    assert pipeline["explicit"] == "/env/python3.12"  # env reached find_python
    assert _stdout_json(capsys)["python"]["source"] == "env"
    assert _manifest(forge_home)["python"]["version"]

    monkeypatch.setenv("FORGE_PYTHON", "/env/other")
    rc = fb.run(["--repo", str(repo), "--python", "/flag/py", "--quiet"])
    assert rc == 0
    assert pipeline["explicit"] == "/flag/py"  # --python wins over FORGE_PYTHON
    assert _stdout_json(capsys)["python"]["source"] == "flag"


def test_pipeline_report_carries_provenance_fields(
    fb: Any, repo: Path, forge_home: Path, pipeline: dict, capsys: pytest.CaptureFixture
) -> None:
    rc = fb.run(["--repo", str(repo), "--quiet"])
    assert rc == 0
    receipt = _receipt(forge_home)
    assert receipt["schema"] == "forge/InstallReceipt/v1"
    assert receipt["manifest_valid"] is True
    # fake repo: structure ok, no .git, no checksum manifest
    assert receipt["source_trust"] == "B"
    assert receipt["is_git"] is False
    assert receipt["integrity"] == "unverified"


# --------------------------------------------------------------------------
# smoke() unit behavior
# --------------------------------------------------------------------------


def test_smoke_pass_reports_first_stdout_line(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "forge-lens").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 0, "forge-lens 0.1.0\nmore\n")
    )
    out = fb.smoke("forge-lens", venv, ["--version"], lambda m: None)
    assert out == {"status": "PASS", "version": "forge-lens 0.1.0"}


def test_smoke_fail_on_nonzero_and_oserror(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 3))
    out = fb.smoke("cli", tmp_path / "v", ["--version"], lambda m: None)
    assert out["status"] == "FAIL"

    def _boom(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        raise OSError("gone")

    monkeypatch.setattr(fb, "_run", _boom)
    out = fb.smoke("cli", tmp_path / "v", ["--version"], lambda m: None)
    assert out["status"] == "FAIL"
    assert "gone" in out["detail"]


def test_smoke_empty_stdout_is_fail_not_crash(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rc=0 with empty stdout is FAIL — a `--version` that prints nothing
    never proved the CLI works (and must not write `version: ""` into
    the manifest). Never an IndexError escaping Refusal discipline."""
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 0, "  \n"))
    out = fb.smoke("cli", tmp_path / "v", ["--version"], lambda m: None)
    assert out["status"] == "FAIL"
    assert out["version"] == ""


# --------------------------------------------------------------------------
# load_config / create_venv / write_launcher — stage units
# --------------------------------------------------------------------------


def test_load_config_missing_file_refused(fb: Any, tmp_path: Path) -> None:
    with pytest.raises(fb.Refusal) as exc:
        fb.load_config(tmp_path / "nope")
    assert exc.value.kind == "FORGE-INSTALL-NO-CONFIG"


def test_load_config_bad_json_and_missing_keys_refused(fb: Any, tmp_path: Path) -> None:
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "forge.json").write_text("{oops", encoding="utf-8")
    with pytest.raises(fb.Refusal) as exc:
        fb.load_config(bad)
    assert exc.value.kind == "FORGE-INSTALL-BAD-CONFIG"

    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / "forge.json").write_text(json.dumps({"forge_id": "x"}), encoding="utf-8")
    with pytest.raises(fb.Refusal) as exc:
        fb.load_config(partial)
    assert exc.value.kind == "FORGE-INSTALL-BAD-CONFIG"
    assert "package" in exc.value.detail


def test_load_config_valid(fb: Any, repo: Path) -> None:
    cfg = fb.load_config(repo)
    assert cfg["forge_id"] == "forge-lens"
    assert cfg["distribution"] == "forge-lens"


def test_create_venv_reuses_existing(fb: Any, tmp_path: Path, monkeypatch) -> None:
    for i, name in enumerate((".venv", "venv")):
        root = tmp_path / f"root{i}"
        cand = root / name
        py = fb._venv_python(cand)
        py.parent.mkdir(parents=True)
        py.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            fb, "_run", lambda cmd, timeout=300, **kw: pytest.fail("must not spawn")
        )
        assert fb.create_venv(root, "python3", lambda m: None) == cand


def test_create_venv_delegates_explicit_python(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    venv_dir = tmp_path / "root" / ".venv"

    def fake_run(cmd: list[str], timeout: int = 300, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(cmd)
        py = fb._venv_python(venv_dir)
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("x", encoding="utf-8")
        return _cp(cmd)

    monkeypatch.setattr(fb, "_run", fake_run)
    out = fb.create_venv(tmp_path / "root", "/custom/py3.11", lambda m: None)
    assert out == venv_dir
    assert calls[0][:3] == ["/custom/py3.11", "-m", "venv"]


def test_create_venv_delegation_failure_refused(
    fb: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fb, "_run", lambda cmd, timeout=300, **kw: _cp(cmd, 1, "", "nope"))
    with pytest.raises(fb.Refusal) as exc:
        fb.create_venv(tmp_path / "root", "/custom/py3.11", lambda m: None)
    assert exc.value.kind == "FORGE-INSTALL-VENV-FAILED"


def test_write_launcher_no_launcher_is_noop(fb: Any, tmp_path: Path, forge_home: Path) -> None:
    assert fb.write_launcher("cli", tmp_path / "v", True, lambda m: None) is None
    assert not (forge_home / ".local" / "bin").exists()


def test_write_launcher_missing_cli_refused(fb: Any, tmp_path: Path, forge_home: Path) -> None:
    venv = tmp_path / "v"
    (venv / "bin").mkdir(parents=True)
    with pytest.raises(fb.Refusal) as exc:
        fb.write_launcher("ghost", venv, False, lambda m: None)
    assert exc.value.kind == "FORGE-INSTALL-CLI-MISSING"


def test_write_launcher_writes_executable_shim(
    fb: Any, tmp_path: Path, forge_home: Path
) -> None:
    venv = tmp_path / "v"
    real = fb._venv_bin(venv) / "forge-lens"
    real.parent.mkdir(parents=True)
    real.write_text("#!/bin/sh\n", encoding="utf-8")
    out = fb.write_launcher("forge-lens", venv, False, lambda m: None)
    if os.name == "nt":
        # The .cmd is the returned launcher; the LF sh shim serves Git Bash.
        cmd = forge_home / ".local" / "bin" / "forge-lens.cmd"
        assert out == cmd
        raw = cmd.read_bytes()
        assert raw.startswith(b"@echo off\r\n")
        assert raw.endswith(b"\r\n")
        assert b"\r\r\n" not in raw
        assert str(real).encode() in raw
        shim = (forge_home / ".local" / "bin" / "forge-lens").read_bytes()
        assert shim.startswith(b"#!/bin/sh\n")
        assert b"\r" not in shim
        assert f'exec "{real}"'.encode() in shim
    else:
        expected = forge_home / ".local" / "bin" / "forge-lens"
        assert out == expected
        text = expected.read_text(encoding="utf-8")
        assert text.startswith("#!/bin/sh")
        assert f'exec "{real}"' in text
        assert expected.stat().st_mode & 0o777 == 0o755


# --------------------------------------------------------------------------
# register / write_receipt — the persisted documents
# --------------------------------------------------------------------------


def _register_kwargs(cfg: dict, tmp: Path) -> dict:
    return {
        "cfg": cfg,
        "install_root": tmp / "root",
        "venv": tmp / "v",
        "version": "0.1.0",
        "source": {"kind": "pypi", "spec": "forge-lens==0.1.0"},
        "shim": None,
        "mcp_cmd": ["forge-lens", "mcp-serve"],
        "mcp_name": "forge-lens",
        "hosts": {},
    }


def test_register_writes_sorted_manifest(fb: Any, forge_home: Path, tmp_path: Path) -> None:
    cfg = {"forge_id": "forge-lens", "package": "forge_lens",
           "distribution": "forge-lens", "cli": "forge-lens",
           "python": ">=3.11"}
    path = fb.register(**_register_kwargs(cfg, tmp_path))
    assert path == forge_home / ".forge" / "installations" / "forge-lens.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["schema"] == "forge/InstallationManifest/v1"
    assert doc["installation_method"] == "pypi-wheel"  # derived from source.kind
    assert doc["mcp"]["verified"] is False
    assert doc["cli"]["version_cmd"] == ["forge-lens", "--version"]
    assert "environment" not in doc  # absent when not passed


def test_write_receipt_hidden_bootstrap_file(fb: Any, forge_home: Path) -> None:
    cfg = {"forge_id": "forge-lens"}
    receipt = {"schema": "forge/InstallReceipt/v1", "status": "completed"}
    path = fb.write_receipt(cfg, receipt)
    assert path.name == ".forge-lens-bootstrap.json"
    assert json.loads(path.read_text(encoding="utf-8")) == receipt


# --------------------------------------------------------------------------
# environment contract + shell persistence
# --------------------------------------------------------------------------


def test_environment_contract_variables(fb: Any, tmp_path: Path) -> None:
    cfg = {"forge_id": "forge-lens", "cli": "forge-lens"}
    venv = tmp_path / "v"
    cli = venv / "bin" / "forge-lens"
    cli.parent.mkdir(parents=True)
    cli.write_text("x", encoding="utf-8")
    doc = fb.environment_contract(
        cfg, tmp_path / "repo", tmp_path / "root", venv, tmp_path / "shim", has_checkout=True
    )
    assert doc["schema"] == "forge/InstallEnvironment/v1"
    assert doc["prefix"] == "FORGE_LENS"
    values = doc["variables"]
    assert values["FORGE_LENS_HOME"] == str((tmp_path / "repo").resolve())
    assert values["FORGE_LENS_VENV"] == str(venv.resolve())
    assert values["FORGE_LENS_LAUNCHER"] == str((tmp_path / "shim").resolve())
    assert doc["execution_environment"] == (
        "windows" if os.name == "nt" else "posix"
    )


def test_environment_contract_dist_drops_home(fb: Any, tmp_path: Path) -> None:
    cfg = {"forge_id": "forge-lens", "cli": "forge-lens"}
    doc = fb.environment_contract(
        cfg, tmp_path / "repo", tmp_path / "root", tmp_path / "v", None, has_checkout=False
    )
    assert "FORGE_LENS_HOME" not in doc["variables"]
    assert "FORGE_LENS_LAUNCHER" not in doc["variables"]


def test_env_prefix_shell_safe(fb: Any) -> None:
    assert fb.env_prefix("spark-forge-aws") == "SPARK_FORGE_AWS"
    assert fb.env_prefix("a.b c") == "A_B_C"


def test_persist_posix_writes_marked_env_block(
    fb: Any, forge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shim = Path("C:/shimdir") if os.name == "nt" else Path("/shimdir")
    env_file = fb._persist_posix({"FORGE_LENS_VENV": "/v env"}, shim)
    env_path = Path(env_file)
    text = env_path.read_text(encoding="utf-8")
    assert text.count("# >>> forjas account environment >>>") == 1
    assert "export FORGE_LENS_VENV='/v env'" in text
    assert str(shim) in text
    assert "PATH" in text
    if os.name != "nt":
        assert env_path.stat().st_mode & 0o777 == 0o600
    rc = (forge_home / ".bashrc").read_text(encoding="utf-8")
    assert rc.count("forjas-account-environment") == 1
    # process env + PATH are updated for the current session (patched dict)
    assert os.environ["FORGE_LENS_VENV"] == "/v env"
    assert str(shim) in os.environ["PATH"].split(os.pathsep)


def test_persist_posix_is_idempotent(fb: Any, forge_home: Path) -> None:
    shim = Path("C:/shimdir") if os.name == "nt" else Path("/shimdir")
    fb._persist_posix({"FORGE_LENS_VENV": "/v1"}, shim)
    env_file = fb._persist_posix({"FORGE_LENS_VENV": "/v2"}, shim)
    text = Path(env_file).read_text(encoding="utf-8")
    assert text.count("# >>> forjas account environment >>>") == 1
    assert "FORGE_LENS_VENV=/v2" in text
    assert "/v1" not in text
    rc = (forge_home / ".bashrc").read_text(encoding="utf-8")
    assert rc.count("forjas-account-environment") == 1  # init line not duplicated


def test_persist_environment_posix_marks_shell_init(
    fb: Any, forge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = {"variables": {"FORGE_LENS_VENV": "/v"}, "windows_aliases": {}}
    if os.name == "nt":
        # Native Windows persists via HKCU\Environment — stub the seam so
        # the unit test asserts the dispatch without a real registry write.
        seen: dict[str, Any] = {}
        monkeypatch.setattr(
            fb,
            "_persist_windows",
            lambda values, aliases, shim: seen.update(
                values=values, aliases=aliases, shim=shim
            ),
        )
        out = fb.persist_environment(env, None, lambda m: None)
        assert seen["values"] == {"FORGE_LENS_VENV": "/v"}
        assert out["persistence"].startswith("HKCU\\Environment")
        if not fb._is_git_bash():
            assert out["shell_init"] is None
    else:
        out = fb.persist_environment(env, None, lambda m: None)
        assert out["persistence"] == "shell-init"
        assert out["shell_init"].endswith("env.sh")


def test_persist_environment_checked_wraps_os_errors(
    fb: Any, forge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(environment: dict, shim: Path | None, log: Any) -> dict:
        raise OSError("denied")

    monkeypatch.setattr(fb, "persist_environment", _fail)
    with pytest.raises(fb.Refusal) as exc:
        fb._persist_environment_checked({"variables": {}}, None, lambda m: None)
    assert exc.value.kind == "FORGE-ENV-PERSISTENCE-FAILED"
    assert "OSError" in exc.value.detail


def test_atomic_write_leaves_no_tempfile(fb: Any, tmp_path: Path) -> None:
    target = tmp_path / "sub" / "f.json"
    fb._atomic_write(target, b"{}")
    assert target.read_bytes() == b"{}"
    assert [p.name for p in target.parent.iterdir()] == ["f.json"]


def test_detect_hosts_observation_shape(fb: Any) -> None:
    out = fb.detect_hosts()
    assert set(out) == {"claude", "devin", "codex", "copilot"}
    for entry in out.values():
        assert isinstance(entry["cli"], bool)
        assert isinstance(entry["config_dirs"], list)
        assert isinstance(entry["activatable"], bool)


def test_path_guidance_none_when_shim_on_path(
    fb: Any, forge_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(fb.shim_dir()) + os.pathsep + "/usr/bin")
    assert fb._path_guidance() is None
    monkeypatch.setenv("PATH", "/usr/bin")
    assert isinstance(fb._path_guidance(), str)

