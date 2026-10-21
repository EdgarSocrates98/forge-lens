# Referência — API HTTP (v1)

Base: `http://127.0.0.1:7777/v1`. Todos os endpoints retornam JSON.
Auth: quando `api_key` está configurada, os endpoints **de ingestão** exigem
header `X-Forge-Lens-Key`; leitura é aberta (modelo local).

## Ingestão

### `POST /v1/traces`
Envelope nativo `TraceIngest` (`schemas/v1/trace-ingest.json`).
`project_id` do envelope é aplicado às traces.
Resposta: `{accepted_spans, updated_spans, duplicate_spans, traces[],
sanitize{}, warnings[]}`.
Erros: `413` payload/limite excedido, `422` envelope inválido ou span com
`trace_id` divergente, `429` rate limit.

`updated_spans` conta re-entregas **alteradas** mescladas no store
(ex.: `end_time` tardio); `duplicate_spans` conta re-entregas idênticas.

### `POST /v1/otlp/traces`
`ExportTraceServiceRequest` **JSON** (OTLP/HTTP). Protobuf não é aceito.
Resposta: `{accepted_spans, updated_spans, duplicate_spans, traces[],
warnings[]}` — spans malformados são *quarantined*: descartados com
motivo listado em `warnings`, enquanto os válidos são ingeridos.
Erros: `400` JSON inválido, `413` payload/limite excedido, `422` forma OTLP
inválida, `429` rate limit.

## Consulta

### `GET /v1/traces?project_id&session_id&status&limit≤500&offset`
`{items:[{trace_id, project_id, session_id, root_span_id, started_at,
ended_at, status, span_count, error_count}], limit, offset}`.

### `GET /v1/traces/{trace_id}?project_id`
Contrato `Trace` completo ou `404`. Sem `project_id`, a leitura cruza
projetos (modelo local single-user — `project_id` particiona dados, não
é fronteira de segurança).

## Ops

### `GET /v1/health`
`{status:"ok", version, storage:"sqlite", stats:{traces, spans,
error_spans, db_size_bytes, schema_version}}`.

### `GET /v1/metrics`
Snapshot de self-observability: counters (`lens.api.requests`,
`lens.ingest.spans`, `lens.ingest.traces`, `lens.ingest.duplicates`,
`lens.ingest.rejected`, `lens.sanitize.*`, `lens.api.errors`),
gauges e histogramas de latência (`lens.ingest.latency_ms`).
