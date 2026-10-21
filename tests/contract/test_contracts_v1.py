"""Contract tests: schema shape, defaults, enums, unknown-field policy."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from forge_lens.contracts.jsonschema import MODELS_V1, export_schemas
from forge_lens.contracts.provenance import Provenance
from forge_lens.contracts.v1 import (
    SCHEMA_VERSION_V1,
    AgentExecution,
    CostObservation,
    EvaluationResult,
    Evidence,
    Span,
    TokenUsage,
    ToolInvocation,
    Trace,
    TraceIngest,
)

pytestmark = pytest.mark.contract


def test_all_models_have_schema_version() -> None:
    for name, model in MODELS_V1.items():
        assert "schema_version" in model.model_fields, name
        assert model.model_fields["schema_version"].default == SCHEMA_VERSION_V1


def test_schemas_exportable_and_versioned() -> None:
    schemas = export_schemas()
    assert set(schemas) == set(MODELS_V1)
    for name, schema in schemas.items():
        assert schema["x-schema-version"] == SCHEMA_VERSION_V1, name
        assert schema["$id"].endswith(f"/v1/{name}.json")
        assert "properties" in schema


def test_span_id_format_enforced() -> None:
    from datetime import UTC, datetime

    with pytest.raises(ValidationError):
        Span(trace_id="not-hex", span_id="x" * 16, name="s",
             start_time=datetime.now(UTC))
    with pytest.raises(ValidationError):
        Span(trace_id="a" * 32, span_id="short", name="s",
             start_time=datetime.now(UTC))


def test_unknown_fields_preserved() -> None:
    """Newer producers may add fields; consumers must not drop them."""
    from datetime import UTC, datetime

    s = Span(
        trace_id="a" * 32, span_id="b" * 16, name="s",
        start_time=datetime.now(UTC), future_field_v2={"x": 1},
    )
    dumped = s.model_dump()
    assert dumped["future_field_v2"] == {"x": 1}


def test_provenance_is_explicit_enum() -> None:
    assert Provenance.INFERRED.value == "inferred"
    # INFERRED must remain distinguishable from OBSERVED — the epistemic spine.
    assert Provenance.INFERRED != Provenance.OBSERVED


def test_token_usage_no_invented_precision() -> None:
    usage = TokenUsage()
    assert usage.input_tokens is None
    assert usage.measurement_source.value == "unknown"
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-5)


def test_cost_observation_separates_estimated_and_billed() -> None:
    cost = CostObservation(estimated_cost=0.01, billed_cost=0.012)
    assert cost.estimated_cost != cost.billed_cost
    assert cost.provenance == Provenance.DERIVED


def test_models_constructible_minimal() -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    Span(trace_id="a" * 32, span_id="b" * 16, name="x", start_time=now)
    Trace(trace_id="a" * 32, started_at=now, spans=[])
    TraceIngest(traces=[])
    AgentExecution(agent_id="a", run_id="r")
    ToolInvocation(tool_name="t")
    TokenUsage()
    CostObservation()
    Evidence(evidence_id="e1", source="test", timestamp=now)
    EvaluationResult(evaluator="exact-match", metric="accuracy")
