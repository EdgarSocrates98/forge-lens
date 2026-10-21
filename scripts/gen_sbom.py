#!/usr/bin/env python3
"""SBOM generator — CycloneDX 1.5 JSON from the actual environment.

Reads installed distributions (importlib.metadata) so the SBOM is
generated from what's *installed*, not a hand-maintained list.
Usage: python scripts/gen_sbom.py > sbom.json
"""
from __future__ import annotations

import json
from importlib.metadata import distributions


def main() -> None:
    comps = []
    for d in sorted(distributions(), key=lambda x: (x.metadata["Name"] or "").lower()):
        name = d.metadata["Name"]
        if not name:
            continue
        comps.append({
            "type": "library",
            "bom-ref": f"pkg:pypi/{name.lower()}@{d.version}",
            "name": name,
            "version": d.version,
            "purl": f"pkg:pypi/{name.lower()}@{d.version}",
        })
    print(json.dumps({
        "bomFormat": "CycloneDX", "specVersion": "1.5",
        "version": 1,
        "metadata": {"component": {
            "type": "application", "name": "forge-lens",
            "version": "0.1.0"}},
        "components": comps,
    }, indent=2))


if __name__ == "__main__":
    main()
