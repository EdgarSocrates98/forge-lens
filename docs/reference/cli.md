# Referência — CLI `forge-lens`

```bash
forge-lens --help          # ajuda geral
forge-lens --version
```

## `forge-lens init [path] --project <id>`

Cria `.forge-lens/` com `config.toml` e `artifacts/` no diretório.

## `forge-lens serve [--host 127.0.0.1] [--port 7777] [--db <path>]`

Sobe a API (FastAPI+uvicorn). Requer o extra `server`
(`uv sync --extra server`). Endpoints em `docs/reference/api.md`.
Bind fora de loopback sem `api_key` configurada emite um aviso no
stderr — leituras de `/v1/traces*` são abertas por design local.

## `forge-lens ingest <file> [--db] [--project] [--format native|otlp|auto]`

Ingere um JSON local (envelope `TraceIngest` ou `ExportTraceServiceRequest`
OTLP) direto no store — sem servidor. `--format auto` detecta OTLP pela
presença de `resourceSpans`; valor fora de `native|otlp|auto` → exit 2.
Import de arquivo marca as traces `provenance=imported` (vs `observed`
da ingestão viva) — `--format native` honra a provenance do envelope.

## `forge-lens traces list [--db] [--project] [--status ok|error|unset] [--limit N] [--json]`

Lista traces mais recentes com status, span_count e error_count.

## `forge-lens traces show <trace_id> [--db] [--project] [--json]`

Árvore de spans indentada: duração, `!` em erro, `~ flag:` de integridade,
`~ tokens:` de uso. `--json` emite o contrato completo.

## `forge-lens graph <trace_id> [--db] [--project] [--json]`

Grafo de execução (Cycle 03): stats de nós/arestas/profundidade,
critical path (cadeia que domina o wall-clock) e top self-time.
`--json` emite o `ExecutionGraph` completo.

## `forge-lens cost <trace_id> [--db] [--project] [--json]`

Relatório de custo derivado (Cycle 04): tokens totais, breakdown por
modelo, `estimated` em USD, modelos sem preço e findings de desperdício
(`retry_amplification`, `context_replay`, `duplicate_delivery`,
`unknown_tokens`, `unpriced_model`). `--json` inclui `waste[]`.

## `forge-lens incident <trace_id> [--db] [--project] [--json]`

Bundle de incidente (Cycle 05): categoria de falha com evidência,
critical path, timeline de replay e custo estimado — em um único payload.
`--json` emite o bundle completo (`bundle_version` versionado).

## `forge-lens eval <trace_id> [--db] [--project] [--budget-ms N] [--json]`

Bateria de avaliadores determinísticos (Cycle 06): `error_free` +
`tool_success` sempre; `latency_budget` quando `--budget-ms` é passado.
Score `n/a` + motivo quando a evidência é insuficiente — nunca inventa
veredito.

## `forge-lens doctor [path] [--db]`

Verifica Python ≥3.11, diretório de dados, store legível (schema_version,
contagens), extras de servidor e import do SDK. Exit 0/1.

## Configuração

Ordem: argumento > env (`FORGE_LENS_*`) > `.forge-lens/config.toml` > default.
Chaves: `project_id`, `db_path`, `capture` (`metadata|full`), `api_key`,
`auth_reads` (bool — exige a chave também nos GETs), `endpoint`,
`pricing_file` (JSON de preços para `cost`/API), `max_payload_bytes`,
`max_traces_per_request`, `max_spans_per_request`, `max_events_per_span`,
`max_attr_value_chars`, `rate_limit_per_minute`. Env: `FORGE_LENS_PROJECT`,
`_DB`, `_CAPTURE`, `_API_KEY`, `_AUTH_READS`, `_ENDPOINT`, `_PRICING_FILE`,
`_MAX_PAYLOAD_BYTES`, `_MAX_TRACES_PER_REQUEST`, `_MAX_SPANS_PER_REQUEST`,
`_MAX_EVENTS_PER_SPAN`, `_MAX_ATTR_VALUE_CHARS`, `_RATE_LIMIT_PER_MINUTE` —
valor malformado → `invalid env FORGE_LENS_*=…`.

`serve` deriva o bind de `endpoint` (host+port) quando `--host`/`--port`
não são passados — `endpoint` é a config que diz onde esta instância
escuta *e* onde clientes a alcançam.
