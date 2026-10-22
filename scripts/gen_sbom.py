#!/usr/bin/env python3
"""SBOM generator — CycloneDX 1.5 JSON from the actual environment.

Reads installed distributions (importlib.metadata) so the SBOM is
generated from what's *installed*, not a hand-maintained list.
Usage: python scripts/gen_sbom.py > sbom.json
"""
from __future__ import annotations

import json
import re
from importlib.metadata import distributions, version


def _purl(name: str, ver: str) -> str:
    # purl spec requires the PEP 503 normalized name: lowercase with runs
    # of `-`, `_`, `.` collapsed to a single hyphen (e.g. `importlib_metadata`
    # -> `importlib-metadata`). A bare `name.lower()` emits invalid purls.
    norm = re.sub(r"[-_.]+", "-", name).lower()
    return f"pkg:pypi/{norm}@{ver}"


def main() -> None:
    comps = []
    for d in sorted(distributions(), key=lambda x: (x.metadata["Name"] or "").lower()):
        name = d.metadata["Name"]
        if not name:
            continue
        purl = _purl(name, d.version)
        comps.append({
            "type": "library",
            "bom-ref": purl,
            "name": name,
            "version": d.version,
            "purl": purl,
        })
    print(json.dumps({
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {
            "type": "application", "name": "forge-lens",
            "version": version("forge-lens")}},
        "components": comps,
    }, indent=2))


if __name__ == "__main__":
    main()
