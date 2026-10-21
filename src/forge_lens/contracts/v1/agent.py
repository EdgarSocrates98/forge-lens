"""Agent and tool execution contracts (v1)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from forge_lens.contracts.base import LensModel
from forge_lens.contracts.provenance import Provenance
from forge_lens.contracts.v1.refs import BlobRef
from forge_lens.contracts.v1.trace import Status


class ToolInvocation(LensModel):
    """One call from an agent to a tool."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    tool_name: str
    tool_call_id: str | None = None
    """Provider-assigned call id, when observed."""

    input_ref: BlobRef | None = None
    output_ref: BlobRef | None = None
    started_at: datetime | None = None
    duration_ms: float | None = None
    status: Status = Status()
    retry_count: int = 0
    error: str | None = None
    permissions_metadata: dict[str, str] = Field(default_factory=dict)
    provenance: Provenance = Provenance.OBSERVED


class AgentExecution(LensModel):
    """A run of an agent inside a session/workflow."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    agent_id: str
    agent_name: str | None = None
    run_id: str
    parent_run_id: str | None = None
    """Set only when delegation was actually observed — never guessed."""

    host: str | None = None
    model: str | None = None
    provider: str | None = None
    status: Status = Status()
    capabilities: list[str] = Field(default_factory=list)
    tool_calls: list[ToolInvocation] = Field(default_factory=list)
    input_ref: BlobRef | None = None
    output_ref: BlobRef | None = None
    trace_id: str | None = None
    """Link to the trace that contains this execution's spans."""

    started_at: datetime | None = None
    ended_at: datetime | None = None
    provenance: Provenance = Provenance.OBSERVED
