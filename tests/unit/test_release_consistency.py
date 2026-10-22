"""Release-surface consistency guards.

Regression coverage for the cycle-13.1 release audit (`docs/release-audit.md`):
the support matrix declared in metadata must match what CI actually tests, the
package version must match the wheel version, and the SBOM generator must emit
canonical (PEP 503) purls. All checks are file-level — no network, no CI.
"""

from __future__ import annotations

import importlib.util
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
SDK_DIR = ROOT / "packages" / "forge-lens-sdk-ts"
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _sdk_package() -> dict:
    return json.loads((SDK_DIR / "package.json").read_text(encoding="utf-8"))


def _gen_sbom():
    spec = importlib.util.spec_from_file_location(
        "gen_sbom", ROOT / "scripts" / "gen_sbom.py"
    )
    assert spec
    assert spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_version_matches_package() -> None:
    """pyproject version == runtime __version__ (release gate consistency)."""
    import forge_lens

    assert _pyproject()["project"]["version"] == forge_lens.__version__


def test_console_script_target_importable() -> None:
    """[project.scripts] entry point must resolve to a callable attribute."""
    target = _pyproject()["project"]["scripts"]["forge-lens"]
    module_name, attr = target.split(":")
    module = __import__(module_name, fromlist=[attr])
    assert callable(getattr(module, attr))


def test_python_matrix_covers_declared_classifiers() -> None:
    """Every declared Python classifier is exercised by the CI matrix."""
    classifiers = _pyproject()["project"]["classifiers"]
    declared = {
        m.group(1)
        for c in classifiers
        if (m := re.fullmatch(r"Programming Language :: Python :: (\d+\.\d+)", c))
    }
    matrix = set(
        re.search(r"python: \[([^\]]+)\]", CI_YML.read_text(encoding="utf-8"))
        .group(1)
        .replace('"', "")
        .split(", ")
    )
    assert declared <= matrix, f"classifiers {declared} not covered by matrix {matrix}"


def test_python_matrix_satisfies_requires_python() -> None:
    """No matrix version may fall outside `requires-python`."""
    floor = re.fullmatch(
        r">=(\d+\.\d+)", _pyproject()["project"]["requires-python"].strip()
    ).group(1)
    matrix = re.search(r"python: \[([^\]]+)\]", CI_YML.read_text(encoding="utf-8")).group(1)
    for v in re.findall(r"\d+\.\d+", matrix):
        assert tuple(map(int, v.split("."))) >= tuple(map(int, floor.split(".")))


def test_sdk_engines_floor_matches_ci_matrix() -> None:
    """engines.node floor == lowest Node version exercised in CI."""
    engines = _sdk_package()["engines"]["node"]
    floor = int(re.fullmatch(r">=(\d+)", engines.strip()).group(1))
    matrix_block = re.search(
        r"node: \[([^\]]+)\]", CI_YML.read_text(encoding="utf-8")
    ).group(1)
    matrix = sorted(int(v) for v in re.findall(r"\d+", matrix_block))
    assert floor == matrix[0]


def test_license_metadata_declared() -> None:
    """Both published artifacts declare SPDX MIT in their metadata."""
    assert _pyproject()["project"]["license"] == "MIT"
    assert _sdk_package()["license"] == "MIT"


def test_sbom_purl_is_pep503_normalized() -> None:
    """purls must use normalized names — `importlib_metadata`, not verbatim."""
    purl = _gen_sbom()._purl
    assert purl("importlib_metadata", "1.0") == "pkg:pypi/importlib-metadata@1.0"
    assert purl("Zope.Interface", "2.0") == "pkg:pypi/zope-interface@2.0"
    assert purl("plain-name", "3") == "pkg:pypi/plain-name@3"
