"""Trace projection for KnowledgeContextPack provenance.

Lens records that a context pack was used without copying document contents
into telemetry.  Only stable identifiers, versions and degradation state are
attached to the trace resource attributes.
"""

from __future__ import annotations

from typing import Any

from forge_lens.contracts.v1.trace import Trace

KNOWLEDGE_CONTEXT_PACK_SCHEMA = "theforge/KnowledgeContextPack/v1"


def attach_context_pack_provenance(
    trace: Trace, pack: dict[str, Any], *, authorized: bool
) -> Trace:
    """Return a trace with redacted, auditable context-pack metadata."""
    if pack.get("schema") != KNOWLEDGE_CONTEXT_PACK_SCHEMA:
        raise ValueError("unsupported knowledge context pack schema")
    if not authorized:
        raise ValueError("cannot attach an unauthorized knowledge context pack")
    attrs = dict(trace.resource_attributes)
    attrs.update(
        {
            "knowledge.pack_id": str(pack.get("pack_id", "")),
            "knowledge.workspace_id": str(pack.get("workspace_id", "")),
            "knowledge.estimated_tokens": int(pack.get("estimated_tokens", 0)),
            "knowledge.budget_tokens": int(pack.get("budget_tokens", 0)),
            "knowledge.degraded_state": str(pack.get("degraded_state", "none")),
            "knowledge.source_versions": dict(pack.get("source_versions") or {}),
            "knowledge.selected_items": len(pack.get("selected_items") or []),
        }
    )
    return trace.model_copy(update={"resource_attributes": attrs})
