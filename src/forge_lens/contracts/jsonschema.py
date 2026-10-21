"""Export versioned JSON Schemas for the public contracts.

Schemas are generated from the Pydantic models (single source of truth)
and committed under ``schemas/v<N>/`` so external consumers can validate
payloads without installing the package.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from forge_lens.contracts.base import LensModel
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

MODELS_V1: dict[str, type[LensModel]] = {
    "trace": Trace,
    "trace-ingest": TraceIngest,
    "span": Span,
    "agent-execution": AgentExecution,
    "tool-invocation": ToolInvocation,
    "token-usage": TokenUsage,
    "cost-observation": CostObservation,
    "evidence": Evidence,
    "evaluation-result": EvaluationResult,
}


def export_schemas(models: dict[str, type[LensModel]] | None = None) -> dict[str, dict[str, Any]]:
    """Return ``{name: json_schema_dict}`` for the given models (v1 default)."""
    models = models or MODELS_V1
    out: dict[str, dict[str, Any]] = {}
    for name, model in models.items():
        schema = model.model_json_schema()
        schema["$id"] = f"https://forge-lens.dev/schemas/v1/{name}.json"
        schema["x-schema-version"] = SCHEMA_VERSION_V1
        out[name] = schema
    return out


def write_schemas(target_dir: Path) -> list[Path]:
    """Write one ``<name>.json`` per model into ``target_dir``. Returns files."""
    target_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, schema in export_schemas().items():
        path = target_dir / f"{name}.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written


if __name__ == "__main__":
    # parents[3] resolves <repo>/src/forge_lens/contracts/jsonschema.py → repo
    # root. This assumes execution from a source checkout (which is how CI
    # runs it); from an installed site-packages layout the path is wrong.
    root = Path(__file__).resolve().parents[3]
    for p in write_schemas(root / "schemas" / "v1"):
        print(p)
