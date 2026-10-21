# Referência — Contratos v1

Fonte única: `src/forge_lens/contracts/v1/` (Pydantic). JSON Schemas
gerados: `schemas/v1/*.json` — regenere com
`python -m forge_lens.contracts.jsonschema`.

## Regras de compatibilidade

- `schema_version: "1.0.0"` em todo modelo — bump obrigatório em quebra.
- `extra="allow"`: campos desconhecidos são preservados — consumidores antigos
  leem payloads novos sem perda.
- Campos ausentes ficam `None` — nunca valores fabricados.

## Modelos

### `Trace` / `TraceIngest`
`trace_id` (32-hex), `project_id`, `session_id`, `root_span_id`,
`started_at`/`ended_at` (ISO-8601), `status{code,message}`, `source`
(`TraceSource`: system, sdk_name, sdk_version, runtime, language, host),
`resource_attributes`, `spans[]`, `provenance`.

### `Span`
`span_id` (16-hex), `parent_span_id`, `name`, `kind` (OTel + OpenInference),
`start_time`/`end_time`, `attributes`, `events[]`, `links[]`, `status`,
`provenance`, `integrity_flags[]`.

`SpanKind`: `internal|server|client|producer|consumer|agent|chain|llm|tool|
retriever|embedding|reranker|guardrail|evaluator|other`.

`IntegrityFlag`: `missing_parent|orphan|out_of_order|duplicate|partial_trace|
clock_skew|truncated|unknown_tokens|missing_end`.

### `TokenUsage`
`input_tokens|output_tokens|cached_input_tokens|reasoning_tokens|
total_tokens` (≥0, opcionais), `provider`, `model`,
`measurement_source: provider|estimated|unknown`.

### `CostObservation`
`currency` (ISO-4217), `estimated_cost` (derived), `billed_cost` (observed),
`pricing_source`, `pricing_version`, `usage`, `assumptions[]`.

### `AgentExecution` / `ToolInvocation`
`agent_id`, `run_id`, `parent_run_id` (só quando observado), `model`,
`provider`, `capabilities[]`, `tool_calls[]`, `input_ref`/`output_ref`
(`BlobRef`). Tool: `tool_name`, `tool_call_id`, `retry_count`, `error`,
`permissions_metadata`.

### `Evidence`
`evidence_id`, `source`, `origin_type` (Provenance), `timestamp`, `hash`,
`reference`, `classification` (DataClassification),
`sanitization_status` (`none|redacted|hashed|truncated|dropped`).

### `EvaluationResult`
`evaluator`, `evaluator_version`, `dataset`, `dataset_version`, `metric`,
`score`, `confidence`, `ground_truth_reference`, `trace_reference`,
`evaluated_at`, `details`.

### `BlobRef`
`kind: inline|uri|sha256|artifact|dropped`, `value`, `media_type`,
`size_bytes`, `sha256`, `metadata`.

### `Provenance`
`observed|imported|derived|inferred|unknown` — a espinha epistêmica.
