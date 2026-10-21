# Referência — API HTTP (v1)

Base: `http://127.0.0.1:7777/v1`. Todos os endpoints retornam JSON.
Auth: quando `api_key` está configurada, os endpoints **de ingestão** exigem
header `X-Forge-Lens-Key`; leitura é aberta por padrão (modelo local) —
`auth_reads = true` (TOML/`FORGE_LENS_AUTH_READS`) estende a exigência da
chave a todos os GETs de dados (`/v1/traces*`, `/v1/graph/*`,
`/v1/analytics/*`, `/v1/health`, `/v1/metrics`).

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
sanitize{}, warnings[]}` — spans malformados são *quarantined*: descartados com
motivo listado em `warnings`, enquanto os válidos são ingeridos.
Erros: `400` JSON inválido, `413` payload/limite excedido, `422` forma OTLP
inválida, `429` rate limit.

## Consulta

### `GET /v1/traces?project_id&session_id&status&limit≤500&offset`
`{items:[{trace_id, project_id, session_id, root_span_id, started_at,
ended_at, status, span_count, error_count}], limit, offset}`.

### `GET /v1/traces/{trace_id}?project_id`
Contrato `Trace` completo ou `404`. Sem `project_id`, usa o
`cfg.project_id` configurado — a leitura cruzada entre projetos existe
apenas no `Store` interno (modelo local single-user — `project_id`
particiona dados, não é fronteira de segurança).

## Inteligência (Cycles 03/04)

### `GET /v1/graph/traces/{trace_id}?project_id`
Grafo de execução `ExecutionGraph`: nós = spans; arestas `parent`
(contenção observada), `sequence` (sucessão temporal entre irmãos por
`start_time`), `link` (`SpanLink`s — `dangling: true` quando o alvo não
está no grafo). `stats`: `span_count`, `edge_count`, `max_depth`,
`dangling_edges`, `missing_parents`, `cycle_members` (spans sentados
num ciclo de parentesco — parent chains corrompidos são evidência).
`404` se a trace não existe.

### `GET /v1/graph/traces/{trace_id}/critical-path`
`{path: [span_id…], total_ms, cycles: [{from, to}]}` — cadeia
raiz→folha que maximiza a soma de durações, avaliada sobre **todos** os
roots (traces parciais multi-root são cobertas). Ciclos de parentesco
são cortados deterministicamente — a aresta que fecha o ciclo aparece
em `cycles` (evidência), não derruba o endpoint. Cadeias arbitrariamente
profundas são iterativas (sem limite de recursão). Empates desempatam
por id lexicográfico.

### `GET /v1/graph/traces/{trace_id}/bottlenecks?top=5`
`{items: [{id, name, kind, self_ms, duration_ms}]}` — self-time =
duração do span menos a soma dos filhos diretos (clamp ≥ 0; filhos
podem se sobrepor). Ordenado por `self_ms` desc.

### `GET /v1/graph/sessions/{session_id}?project_id&limit=200`
Grafo de sessão: nós = traces ordenadas por `started_at`, arestas
`sequence` entre execuções consecutivas. `stats.truncated` indica que a
sessão excede `limit` (evidência, não erro).

### `GET /v1/traces/{trace_id}/cost`
Relatório derivado (`provenance: "derived"`): `{span_costs[],
total_input_tokens, total_output_tokens, total_tokens,
total_estimated_usd, by_model{}, unpriced_models[],
usage_missing_on_llm_spans[], assumptions[], pricing_source, waste[]}`.
Estimativas vêm do `PricingRegistry` (tabela bundled ou `pricing_file`
JSON); `estimated_usd = null` quando o modelo não é precificado — nunca
inventado. `waste[]` lista findings evidenciados: `retry_amplification`
(≥3 invocações idênticas), `context_replay` (contexto reenviado ≥ maior
janela), `duplicate_delivery` (tokens gastos em spans `duplicate`/
`conflict`, com USD quando precificável), `unknown_tokens` (spans LLM
sem `token_usage`), `unpriced_model`.

### `GET /v1/analytics/pricing`
`{source, models: {model: {input_per_mtok, output_per_mtok,
cached_input_per_mtok}}}` — a tabela efetiva em uso.

## Investigação (Cycle 05)

### `GET /v1/traces/{trace_id}/failure?project_id`
Classificação determinística de falha: `{trace_id, failed, category,
confidence, evidence, detail}`. Categorias: `timeout`, `rate_limit`,
`tool_error`, `llm_error`, `guardrail`, `incomplete` (spans nunca
fechados — host provável morto), `cycle`, `unknown`. Regras correm em
ordem de especificidade; `evidence` nomeia os spans/atributos que
dispararam. `unknown` com `failed=false` é a resposta honesta para
traces saudáveis.

### `GET /v1/traces/{trace_id}/replay?project_id`
Replay **registrado** (não re-executa código do agente): timeline único
ordenado por `(timestamp, span_id, kind)` intercalando
`span_start`/`event`/`span_end` com `offset_ms` relativo ao primeiro
timestamp. `unclosed_spans` conta spans sem `end_time`.

### `GET /v1/traces/{trace_id}/incident?project_id`
Bundle de incidente versionado (`bundle_version`) compondo em um payload
JSON: `failure` + `graph` (stats, critical path, top-5 bottlenecks) +
`cost` + `waste` + `replay`. Um call, todo o evidence — feito para
handoff/export.

## Ops

### `GET /v1/health`
`{status:"ok", version, storage:"sqlite", stats:{traces, spans,
error_spans, db_size_bytes, schema_version}}`.

### `GET /v1/metrics`
Snapshot de self-observability: counters (`lens.api.requests`,
`lens.ingest.spans`, `lens.ingest.traces`, `lens.ingest.duplicates`,
`lens.ingest.rejected`, `lens.sanitize.*`, `lens.api.errors`),
gauges e histogramas de latência (`lens.ingest.latency_ms`).
