from datetime import UTC, datetime

import pytest

from forge_lens.adapters.obsidian import trace_from_obsidian_receipt


def test_obsidian_receipt_becomes_body_free_imported_trace() -> None:
    trace = trace_from_obsidian_receipt(
        {
            "receipt_schema": "theforge/ObsidianSyncReceipt/v1",
            "workspace": "ws-1",
            "vault_id": "vault-1",
            "path": "runbooks/spark.md",
            "content_hash": "a" * 64,
            "sync_state": "updated",
            "classification": "internal",
            "exported_at": "2026-11-09T00:00:00Z",
            "content": "must never cross the adapter boundary",
        },
        project_id="forge-portal",
    )
    assert trace.source.system == "product-forge"
    assert trace.provenance.value == "imported"
    assert trace.started_at == datetime(2026, 11, 9, tzinfo=UTC)
    assert trace.spans[0].attributes["obsidian.path"] == "runbooks/spark.md"
    assert "content" not in trace.spans[0].attributes


@pytest.mark.parametrize("path", ["../outside.md", "/absolute.md", "notes/../../x.md"])
def test_obsidian_adapter_refuses_unsafe_path(path: str) -> None:
    with pytest.raises(ValueError, match="relative"):
        trace_from_obsidian_receipt(
            {
                "receipt_schema": "theforge/ObsidianSyncReceipt/v1",
                "path": path,
                "content_hash": "a" * 64,
                "sync_state": "created",
                "exported_at": "2026-11-09T00:00:00Z",
            }
        )
