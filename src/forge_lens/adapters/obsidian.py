"""Sanitized Forge Lens projection for reviewed Obsidian sync receipts.

The receipt is metadata-only by design.  This adapter never accepts or emits a
note body, absolute vault path, frontmatter, or arbitrary user attributes.
Consumers may send the resulting ``Trace`` through the existing Lens exporter
when they choose to record a reviewed sync operation.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from forge_lens.contracts.provenance import Provenance
from forge_lens.contracts.v1.trace import Span, SpanKind, Status, StatusCode, Trace, TraceSource

_RECEIPT_SCHEMA = "theforge/ObsidianSyncReceipt/v1"
_ALLOWED_STATES = {"created", "updated", "unchanged", "skipped", "denied", "failed", "rolled_back"}


def _id(seed: str, size: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:size]


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Obsidian receipt requires observed exported_at")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(UTC)


def trace_from_obsidian_receipt(
    receipt: Mapping[str, Any], *, project_id: str = "default"
) -> Trace:
    """Convert one validated, body-free receipt into a sanitised Lens trace."""
    if receipt.get("receipt_schema") != _RECEIPT_SCHEMA:
        raise ValueError("unsupported Obsidian receipt schema")
    path = str(receipt.get("path") or "").strip()
    content_hash = str(receipt.get("content_hash") or "").strip()
    state = str(receipt.get("sync_state") or receipt.get("outcome") or "").strip()
    if not path or path.startswith(("/", "\\")) or ".." in path.replace("\\", "/").split("/"):
        raise ValueError("receipt path must be relative")
    if len(content_hash) != 64 or any(ch not in "0123456789abcdef" for ch in content_hash):
        raise ValueError("receipt content_hash must be sha256")
    if state not in _ALLOWED_STATES:
        raise ValueError("unsupported Obsidian sync state")
    observed_at = _timestamp(receipt.get("exported_at"))
    workspace = str(receipt.get("workspace") or "local")
    vault_id = str(receipt.get("vault_id") or "configured")
    seed = f"obsidian:{workspace}:{vault_id}:{path}:{content_hash}"
    trace_id = _id(seed, 32)
    span_id = _id(seed + ":span", 16)
    status = Status(
        code=StatusCode.ERROR if state in {"denied", "failed", "rolled_back"} else StatusCode.OK,
        message=state,
    )
    span = Span(
        trace_id=trace_id,
        span_id=span_id,
        name="obsidian.sync",
        kind=SpanKind.TOOL,
        start_time=observed_at,
        end_time=observed_at,
        status=status,
        provenance=Provenance.IMPORTED,
        attributes={
            "forge.operation": "obsidian.reviewed_sync",
            "obsidian.path": path,
            "obsidian.content_hash": content_hash,
            "obsidian.sync_state": state,
            "obsidian.classification": str(receipt.get("classification") or "internal"),
        },
    )
    return Trace(
        trace_id=trace_id,
        project_id=project_id,
        root_span_id=span_id,
        started_at=observed_at,
        ended_at=observed_at,
        status=status,
        source=TraceSource(system="product-forge", sdk_name="obsidian-receipt-adapter"),
        resource_attributes={"workspace": workspace, "vault_id": vault_id},
        spans=[span],
        provenance=Provenance.IMPORTED,
    )


__all__ = ["trace_from_obsidian_receipt"]
