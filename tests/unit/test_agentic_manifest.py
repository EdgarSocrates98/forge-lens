"""Capability manifest parity and wheel-facing contract tests."""

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _package_version() -> str:
    source = (REPO / "src" / "forge_lens" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*["\']([^"\']+)', source)
    assert match
    return match.group(1)


def test_manifests_match_package_and_each_other() -> None:
    version = _package_version()
    root = json.loads((REPO / "forge.agentic.json").read_text(encoding="utf-8"))
    packaged = json.loads(
        (REPO / "src" / "forge_lens" / "forge.agentic.json").read_text(encoding="utf-8")
    )
    assert root == packaged
    assert root["version"] == version
    assert root["provider"] == "forge-lens"
    assert root["requires_credentials"] is False
    assert root["requires_network"] is False
    assert all(workflow["read_only"] for workflow in root["workflows"])


def test_distribution_manifest_version_matches_package() -> None:
    manifest = json.loads((REPO / "forge.json").read_text(encoding="utf-8"))
    assert manifest["version"] == _package_version()
