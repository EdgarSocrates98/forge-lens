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
  atômico — nunca meia-trace escrita (isolation level default;
  `with conn:` abre transação real, rollback em qualquer falha).
- **Dedupe por `span_hash`**: re-ingestão *idêntica* = `duplicate`
  (e o span guarda `IntegrityFlag.DUPLICATE` como evidência da reentrega).
  Re-entrega com conteúdo **alterado** (ex.: `end_time` tardio, novo
  status, eventos/links adicionados) é **merge**, não duplicata —
  spans parciais podem completar e retentativas pós-crash não viram
  falsos duplicados.
- **Merge determinístico**: por span, `end_time`/`parent` preenchem o que
  faltava, attributes unem (incoming vence), eventos/links unem por
  identidade, status mantém a maior severidade observada
  (`error>ok>unset` — monotônico, um `ok` tardio não apaga um erro
  observado). Na trace: `started=min`, `ended=max`, status e contagens
  recomputados por SELECT sobre os spans persistidos.
- **Flags resolvidas**: `MISSING_END`/`MISSING_PARENT` são removidas
  quando a linha persistida passa a ter `ended_ns`/parent presente —
  flags refletem o estado armazenado, não o histórico.
- **JSON em colunas**: atributos preservam shape exato; consultáveis via
  `json_extract` quando necessário (queries estruturadas ficam nas colunas).
- **`busy_timeout` + `synchronous=NORMAL`**: equilíbrio durabilidade/latência
  apropriado a single-user local.

## Protocolo `TraceStore`

`init_schema`, `upsert_trace→(inserted,updated,duplicates)`, `get_trace`,
`list_traces`, `stats`, `close` — costura estreita para backends futuros
(Parquet export, outro engine) sem tocar o pipeline.

Tempos armazenados como unix-nanos inteiros (`INTEGER NOT NULL` onde o
contrato exige) — conversão por aritmética inteira, sem floats; o store
não lê relógio (`first_seen`/`last_seen` derivam dos dados da trace).

## Retenção

Ver `docs/security/retention.md` — política configurável planejada;
hoje o store cresce até o operador limpar (documentado, sem auto-purge).
