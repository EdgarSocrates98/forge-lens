"""cycle-16.1.1 wave-07d — setup.sh probe + ui/wizard.py parity."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("bash"), reason="bash indisponivel")
def test_setup_sh_python_ausente_recusa_nomeada(tmp_path):
    bash = shutil.which("bash")
    assert bash is not None
    stub = tmp_path / "stubbin"
    stub.mkdir()
    dn = shutil.which("dirname")
    if dn:
        (stub / "dirname").symlink_to(dn)
    out = subprocess.run(  # noqa: S603 — repo script, fixed argv
        [bash, "setup.sh"], cwd=ROOT,
        env={"PATH": str(stub), "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert out.returncode == 2
    assert "FORGE-PYTHON-NOT-FOUND" in out.stderr


def test_vendored_ui_parity_canonical():
    canonical = ROOT.parent / "the-forge" / "src" / "theforge" / "ui"
    if not canonical.is_dir():
        pytest.skip("sibling the-forge ausente")
    for fname in ("kit.py", "i18n.py", "wizard.py"):
        expected = (canonical / fname).read_text(encoding="utf-8").replace(
            "theforge.ui", "forge_lens.ui"
        )
        actual = (ROOT / "src" / "forge_lens" / "ui" / fname).read_text(encoding="utf-8")
        assert actual == expected, f"ui/{fname} drifted"
