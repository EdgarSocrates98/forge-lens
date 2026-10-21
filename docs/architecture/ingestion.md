# Arquitetura — Ingestão

## Entradas suportadas (ciclo 01)

| Entrada | Endpoint | Formato | Estado |
|---|---|---|---|
| Nativa | `POST /v1/traces` | `TraceIngest` (schema v1) | implementado |
| OTLP/HTTP | `POST /v1/otlp/traces` | `ExportTraceServiceRequest` **JSON** | implementado |
| OTLP protobuf | — | — | não implementado (rejeitado explicitamente) |
| Arquivo | `forge-lens ingest <file>` | auto-detect nativo/OTLP | implementado |

## Pipeline (`ingest/pipeline.py`)

```
payload ──► limites de tamanho/spans ──► rate limiter ──►
  por trace:
    cap de eventos/span (→ flag TRUNCATED)
    sanitize_trace()          (capture level + redaction)
    flag_integrity()          (MISSING_PARENT/MISSING_END/CLOCK_SKEW)
    store.upsert_trace()      (dedupe por hash + merge, transação)
───► IngestResult{accepted, updated, duplicates, sanitize, warnings}
```

### Limites (configuráveis, `LensConfig`)

| Parâmetro | Default | Efeito ao exceder |
|---|---|---|
| `max_payload_bytes` | 8 MiB | 413 |
| `max_traces_per_request` | 10 000 | 413 |
| `max_spans_per_request` | 20 000 | 413 |
| `max_events_per_span` | 128 | trunca + flag |
| `max_attr_value_chars` | 4 096 | trunca + marcador |
| `rate_limit_per_minute` | 0 (off) | 429 |

### Dedupe e merge

- Chave de span: `(project_id, trace_id, span_id)` — re-ingestão é idempotente.
- Antes de qualquer escrita, spans cujo `trace_id` difere do `Trace`
  envelope são rejeitados (422) — nada é armazenado sob chave errada.
- Re-entrega idêntica (`span_hash` igual) → `duplicate` + flag
  `DUPLICATE` no span (evidência de retentativa).
- Re-entrega alterada → `updated`: `end_time` tardio, status, atributos,
  eventos e links são mesclados. Spans que "rodavam" podem completar.
- Trace é mesclada em múltiplas flushes: `started_ns=min`, `ended_ns=max`,
  status agrega `error > ok > unset`, `span_count`/`error_count` recomputados.
- Isso suporta exportadores que enviam spans em lotes sem gerar duplicatas.

### Integridade (flags não-fatais)

Anexadas no span, visíveis em `traces show` e na API:

- `missing_parent` — parent referenciado ausente do lote/trace
- `missing_end` — span sem `end_time` (ainda rodando ou perda)
- `clock_skew` — `end_time < start_time`
- `truncated` — eventos cortados pelo cap
- `duplicate` — span re-entregue com conteúdo idêntico (evidência de retry)
- `partial_trace`/`orphan`/`out_of_order`/`unknown_tokens` —
  reservados no enum para regras futuras

### Normalização OTLP (`otlp/normalize.py`)

- Tolerante a `unixNano` como string/int, `AnyValue` em todas as formas,
  seções ausentes (`resourceSpans` vazio → lista vazia, não erro).
- Agrupa por `(resource, trace_id)` — um request pode trazer spans de várias
  traces e recursos.
- Deriva `root_span_id`, bounds temporais e status rollup deterministicamente.
- `openinference.span.kind` sobrepõe o `kind` OTel quando presente.

### Mapeamento OpenInference (`otlp/openinference.py`)

- `openinference.span.kind` → `SpanKind` (AGENT/CHAIN/LLM/TOOL/…).
- Tokens: `llm.token_count.*`, `gen_ai.usage.*`, `llm.usage.*` →
  `attributes["forge_lens.token_usage"]` como `TokenUsage`
  (`measurement_source=provider` quando observado).
- Atributos de conteúdo (`input.value`, `output.value`, `llm.*_messages`,
  `tool.parameters`, `retrieval.documents`) respeitam `capture` level.
