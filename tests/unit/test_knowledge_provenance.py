from datetime import UTC, datetime

import pytest

from forge_lens.contracts.v1.trace import Trace
from forge_lens.knowledge import attach_context_pack_provenance


def _trace() -> Trace:
    return Trace(
        trace_id="a" * 32,
        started_at=datetime.now(UTC),
    )


def test_context_pack_provenance_is_metadata_only() -> None:
    trace = attach_context_pack_provenance(
        _trace(),
        {
            "schema": "theforge/KnowledgeContextPack/v1",
            "pack_id": "pack-1",
            "workspace_id": "ws-1",
            "estimated_tokens": 12,
            "budget_tokens": 100,
            "source_versions": {"atlas": "7"},
            "selected_items": ["item-1"],
            "items": [{"body": "must not enter telemetry"}],
        },
        authorized=True,
    )
    assert trace.resource_attributes["knowledge.pack_id"] == "pack-1"
    assert trace.resource_attributes["knowledge.selected_items"] == 1
    assert "must not enter telemetry" not in str(trace.resource_attributes)


def test_unauthorized_pack_is_rejected() -> None:
    with pytest.raises(ValueError, match="unauthorized"):
        attach_context_pack_provenance(
            _trace(), {"schema": "theforge/KnowledgeContextPack/v1"}, authorized=False
        )
