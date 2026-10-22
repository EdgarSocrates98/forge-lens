#!/usr/bin/env python3
"""Forge Lab emitter — runs the repo's golden scenario corpus
(``tests/golden/scenarios/*.json`` through the real pytest runner) and
emits ``forge/BenchmarkResult/v1``.

Deterministic, offline: every scenario executes in-process — SQLite
tmp stores, the FastAPI ``TestClient``, MCP stdio frames. No servers,
no sockets.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re as _re
import sys
import tempfile
import time
import xml.etree.ElementTree as _ET
from datetime import UTC, datetime
from pathlib import Path

FORGE = "forge-lens"
SUITE = "forge-lens-lab"

_SCENARIO_RE = _re.compile(r"^test_golden_scenario\[(?P<sid>.+)\]$")
_SCENARIO_FILE = "tests/golden/scenarios"
_RUNNER_FILE = "tests/golden/test_golden_scenarios.py"
_DETAIL_LIMIT = 400


def _slug(s: object) -> str:
    return _re.sub(r"[^a-z0-9-]+", "-", str(s).lower()).strip("-") or "scenario"


def m(state: str, value: object = None, unit: str = "") -> dict:
    return {"state": state, "unit": unit, "value": value}


def _row(tc: _ET.Element) -> dict:
    """One ``forge/BenchmarkResult/v1`` row from a junit ``testcase``.

    Golden scenarios are pass/fail verdicts, not ranked detection:
    ``success`` is the measured metric, ``precision``/``recall`` stay
    ``not_available`` rather than fabricating a 0.0. ``forbidden.*``
    assertion hits are the corpus's false-positive surface — counted
    from the runner's own problem list.
    """
    name = tc.get("name", "")
    m_ = _SCENARIO_RE.match(name)
    if m_:
        scenario_id = _slug(m_.group("sid"))
        evidence_ref = f"{_SCENARIO_FILE}/{m_.group('sid')}.json"
    else:
        # corpus guards (presence/uniqueness/schema) run in the same
        # file — a damaged corpus must fail the lab, not slip by.
        scenario_id = _slug(name.removeprefix("test_"))
        evidence_ref = _RUNNER_FILE
    duration_ms = int(float(tc.get("time", "0") or 0) * 1000)

    verdict = "pass"
    detail = ""
    skipped = tc.find("skipped")
    failed = tc.find("failure")
    errored = tc.find("error")
    bad = failed if failed is not None else errored
    if skipped is not None:
        verdict = "skipped"
        detail = (skipped.get("message") or "").strip()[:_DETAIL_LIMIT]
    elif bad is not None:
        verdict = "fail"
        detail = (bad.text or bad.get("message") or "").strip()[:_DETAIL_LIMIT]
    forbidden = (
        len(_re.findall(r"\bforbidden\.", bad.text or ""))
        if bad is not None
        else 0
    )

    return {
        "scenario_id": scenario_id,
        "verdict": verdict,
        "duration_ms": duration_ms,
        "detail": detail,
        "evidence_ref": evidence_ref,
        "judge": None,
        "metrics": {
            "success": m("measured", 1.0 if verdict == "pass" else 0.0, "ratio")
            if verdict != "skipped"
            else m("not_available"),
            "precision": m("not_available"),
            "recall": m("not_available"),
            "false_positives": m("measured", forbidden),
            "cost_usd": m("not_applicable"),
            "tokens": m("not_applicable"),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--checkout", required=True)
    ap.add_argument("--mode", default="A", choices=["A", "B", "C"])
    ap.add_argument("--result-out", required=True)
    a = ap.parse_args()
    checkout = Path(a.checkout).resolve()
    sys.path.insert(0, str(checkout / "src"))

    import pytest

    started = time.monotonic()
    results: list[dict] = []
    categories: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="forge-lens-lab-") as td:
        junit = Path(td) / "junit.xml"
        rc = pytest.main(
            [
                "-q",
                "-p",
                "no:cacheprovider",
                str(checkout / "tests" / "golden"),
                f"--junitxml={junit}",
            ]
        )
        if junit.is_file():
            for tc in _ET.parse(junit).getroot().iter("testcase"):
                results.append(_row(tc))
        for sf in sorted((checkout / _SCENARIO_FILE).glob("*.json")):
            with contextlib.suppress(OSError, json.JSONDecodeError):
                categories.add(json.loads(sf.read_text()).get("category", ""))
        categories.discard("")
    elapsed = int((time.monotonic() - started) * 1000)

    if not results:
        results.append(
            {
                "scenario_id": "corpus-collected",
                "verdict": "fail",
                "duration_ms": elapsed,
                "detail": f"pytest collected no golden corpus tests (rc={rc})",
                "evidence_ref": _RUNNER_FILE,
                "judge": None,
                "metrics": {
                    "success": m("measured", 0.0, "ratio"),
                    "precision": m("not_available"),
                    "recall": m("not_available"),
                    "false_positives": m("not_available"),
                    "cost_usd": m("not_applicable"),
                    "tokens": m("not_applicable"),
                },
            }
        )
    results.sort(key=lambda r: str(r["scenario_id"]))
    n_scenarios = sum(
        1 for r in results if str(r["evidence_ref"]).startswith(_SCENARIO_FILE)
    )
    n_failed = sum(1 for r in results if r["verdict"] == "fail")

    receipt = {
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "external": False,
        "suite": SUITE,
        "forge": FORGE,
        "mode": a.mode,
        "notes": [
            f"scenarios={n_scenarios}",
            f"categories={len(categories)}",
            f"failed={n_failed}",
            f"pytest_rc={rc}",
        ],
        "producer": {"id": FORGE, "version": "lab-script"},
        "results": results,
    }
    out = Path(a.result_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
