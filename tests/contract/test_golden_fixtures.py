"""Golden fixture tests — normalization output must stay byte-stable.

If a contract/normalization change is intentional, regenerate fixtures::

    python -m tests.fixtures.regenerate   # (or re-run the generator script)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from forge_lens.contracts.v1.trace import TraceIngest
from forge_lens.otlp.normalize import normalize_otlp_request

FIXTURES = Path(__file__).parent.parent / "fixtures"

pytestmark = pytest.mark.contract


def test_otlp_normalization_is_stable() -> None:
    raw = json.loads((FIXTURES / "otlp-request.json").read_text())
    golden = json.loads((FIXTURES / "golden-normalized-trace.json").read_text())
    traces = normalize_otlp_request(raw, project_id="golden")
    actual = [t.model_dump(mode="json") for t in traces]
    assert actual == golden


def test_native_fixture_validates() -> None:
    env = TraceIngest.model_validate_json((FIXTURES / "native-ingest.json").read_text())
    assert env.project_id == "fixture"
    assert len(env.traces) == 1
    assert env.traces[0].spans[0].name == "root"
