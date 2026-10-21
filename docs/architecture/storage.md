# Arquitetura — Armazenamento

## SQLite WAL (default, implementado)

**Por quê**: local-first = zero infra, transações ACID, arquivo único
portável, leitura concorrente com escrita (WAL).

### Tabelas

- `lens_meta(key,value)` — `schema_version` do banco (gancho de migração).
- `traces` — PK `(project_id, trace_id)`; resumo denormalizado
  (`span_count`, `error_count`, bounds em ns, status, source JSON,
  `first_seen_ns`/`last_seen_ns` para ingestão incremental).
- `spans` — PK `(project_id, trace_id, span_id)`; colunas quentes
  (name, kind, tempos em ns, status) + JSON canônico para attributes,
  events, links, integrity_flags + `span_hash` (sha256, idempotência).

### Índices

`traces`: `(project_id, session_id)`, `(project_id, started_ns DESC)`,
`(project_id, status_code)`. `spans`: `(project_id, name|kind|status_code)`.

### Decisões

- **Transação por `upsert_trace`**: lote de spans + linha de trace é
  atômico — nunca meia-trace escrita.
- **Dedupe por PK**: re-ingestão idêntica = `duplicate`, não erro.
- **Merge determinístico**: `started=min`, `ended=max`, status com
  precedência `error>ok>unset`, contagens recomputadas por SELECT.
- **JSON em colunas**: atributos preservam shape exato; consultáveis via
  `json_extract` quando necessário (queries estruturadas ficam nas colunas).
- **`busy_timeout` + `synchronous=NORMAL`**: equilíbrio durabilidade/latência
  apropriado a single-user local.

## Protocolo `TraceStore`

`init_schema`, `upsert_trace→(inserted,duplicates)`, `get_trace`,
`list_traces`, `stats`, `close` — costura estreita para backends futuros
(Parquet export, outro engine) sem tocar o pipeline.

## Retenção

Ver `docs/security/retention.md` — política configurável planejada;
hoje o store cresce até o operador limpar (documentado, sem auto-purge).
