"""Cycle 13.1 — the generated MCP tool matrix is a single source of
truth: ``docs/reference/mcp-tools.md`` must be byte-identical to what
``scripts/gen_mcp_matrix.py`` emits from the live registry in
``forge_lens.mcp_server``. Drift fails here, not at review time.

The script is loaded in-process via importlib (it is a stdlib-only
script, not a package) — ``render()`` is regenerated to memory and
compared, never trusting the file on disk as input.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from forge_lens.mcp_server import _tools

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "gen_mcp_matrix.py"
_DOC = _REPO / "docs" / "reference" / "mcp-tools.md"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gen_mcp_matrix", _SCRIPT
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_doc_is_in_sync_with_generator() -> None:
    """Regenerate to memory and compare bytes — the committed doc is
    either current generator output or the test fails."""
    gen = _load()
    assert _DOC.exists(), (
        "docs/reference/mcp-tools.md ausente — rode gen_mcp_matrix.py"
    )
    assert _DOC.read_text() == gen.render()


def test_generator_is_deterministic() -> None:
    gen = _load()
    assert gen.render() == gen.render()


def test_matrix_covers_the_whole_registry() -> None:
    doc = _load().render()
    names = [t["name"] for t in _tools()]
    assert len(names) == len(set(names)), "tool names must be unique"
    for name in names:
        assert f"`{name}`" in doc
    # the "generated" marker must survive regeneration
    assert "GERADO" in doc


def test_check_mode_reports_drift(tmp_path: Path) -> None:
    """--check exits non-zero on a stale file, zero on the real one."""
    gen = _load()
    stale = tmp_path / "stale.md"
    stale.write_text("# outdated\n")
    assert gen.main(["--check", "--out", str(stale)]) == 1
    assert gen.main(["--check", "--out", str(_DOC)]) == 0


def test_generator_cli_stdout_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--stdout prints the doc without touching the file."""
    gen = _load()
    out = tmp_path / "m.md"
    assert gen.main(["--stdout", "--out", str(out)]) == 0
    assert not out.exists()
    assert capsys.readouterr().out == gen.render()
