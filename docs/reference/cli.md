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

## `forge-lens doctor [path] [--db]`

Verifica Python ≥3.11, diretório de dados, store legível (schema_version,
contagens), extras de servidor e import do SDK. Exit 0/1.

## Configuração

Ordem: argumento > env (`FORGE_LENS_*`) > `.forge-lens/config.toml` > default.
Chaves: `project_id`, `db_path`, `capture` (`metadata|full`), `api_key`,
`endpoint`, `max_payload_bytes`, `max_spans_per_request`,
`max_events_per_span`, `max_attr_value_chars`, `rate_limit_per_minute`.
