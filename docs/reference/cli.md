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

## `forge-lens traces list [--db] [--project] [--status ok|error|unset] [--session S] [--tool T] [--q S] [--since ISO] [--until ISO] [--min-duration-ms N] [--max-duration-ms N] [--limit N] [--json]`

Lista traces mais recentes com status, span_count e error_count.
`--tool`/`--q` são substring; `--since`/`--until` limitam `started_at`;
filtros de duração excluem traces sem `ended_at`.

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

## `forge-lens keys create --name <n> [--role reader|writer|admin] [--project P]... [--db]`
## `forge-lens keys list [--db]` · `forge-lens keys revoke <key_id> [--db]`

Ciclo de vida de API keys (Cycle 08): o segredo aparece **uma vez** —
só o hash fica no store. `--project` repetível define o escopo de
tenancy; omitir = chave global.

## `forge-lens prune [--db] [--project P] [--before ISO | --days N] --yes`

Expurgo por idade (Cycle 08). Sem `--before`/`--days` usa
`retention_days` da config; uma das duas fontes é obrigatória.
`--yes` confirma a deleção. Exit 2 sem critério/confirmação.

## `forge-lens archive <trace_id> [--db] [--project] [--out file] [--delete --yes] [--append]`

Arquivamento frio (Cycle 08): exporta JSONL por trace. `--delete` apaga
após exportar — só o que foi exportado. Sem `--append`, recusa
sobrescrever um arquivo existente.

## `forge-lens import-claude <session.jsonl> [--db] [--project claude-code]`

Importa uma sessão Claude Code (JSONL `assistant`/`user` com
`tool_use`/`tool_result` aninhados) como trace derivada de log. Arquivo
limitado a 256 MiB; eventos inválidos → exit 2, zero linhas persistidas.

## `forge-lens mcp-serve [--db]`

Servidor MCP stdio read-only (Cycle 10): abre o store readonly, exit 2
se o DB não existir ou `pricing_file` for inválido. Tools em
`docs/reference/mcp.md`.

## `forge-lens install-status [--data-dir <path>]`

Lê o receipt de instalação (`install/receipt.json`) sem criar
diretórios — operação read-only.

## Configuração

Ordem: argumento > env (`FORGE_LENS_*`) > `.forge-lens/config.toml` > default.
Chaves: `project_id`, `db_path`, `capture` (`metadata|full`), `api_key`,
`auth_mode` (`none|key|keys` — RBAC com roles e escopo por projeto),
`auth_reads` (bool — exige a chave também nos GETs), `endpoint`,
`retention_days` (padrão do `prune`), `pricing_file` (JSON de preços
para `cost`/API), `max_payload_bytes`,
`max_traces_per_request`, `max_spans_per_request`, `max_events_per_span`,
`max_attr_value_chars`, `rate_limit_per_minute`. Env: `FORGE_LENS_PROJECT`,
`_DB`, `_CAPTURE`, `_API_KEY`, `_AUTH_MODE`, `_AUTH_READS`,
`_RETENTION_DAYS`, `_ENDPOINT`, `_PRICING_FILE`,
`_MAX_PAYLOAD_BYTES`, `_MAX_TRACES_PER_REQUEST`, `_MAX_SPANS_PER_REQUEST`,
`_MAX_EVENTS_PER_SPAN`, `_MAX_ATTR_VALUE_CHARS`, `_RATE_LIMIT_PER_MINUTE` —
valor malformado → `invalid env FORGE_LENS_*=…`.

`serve` aceita `--ssl-certfile`/`--ssl-keyfile` para TLS no modo remoto.

`serve` deriva o bind de `endpoint` (host+port) quando `--host`/`--port`
não são passados — `endpoint` é a config que diz onde esta instância
escuta *e* onde clientes a alcançam.
