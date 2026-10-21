# Guia — Runbook de Operações

> Estado: descreve apenas comportamento implementado. Fonte de verdade:
> `src/forge_lens/cli/main.py`, `src/forge_lens/core/config.py`,
> `src/forge_lens/api/app.py`, `src/forge_lens/storage/sqlite.py`.
> Resíduos declarados em `docs/cycles/cycle-06-12.md`.

## 1. Layout de dados

`forge-lens init .` cria `.forge-lens/` no diretório do projeto:

| Path | Conteúdo |
|---|---|
| `.forge-lens/config.toml` | configuração (`[lens]` — chaves abaixo) |
| `.forge-lens/lens.db` | SQLite WAL — criado na **primeira escrita** (ou no primeiro request HTTP, inclusive `GET /v1/health`, que instancia o store lazy) |
| `.forge-lens/lens.db-wal` / `-shm` | sidecars do WAL — existem enquanto o modo WAL está ativo |
| `.forge-lens/alert-rules.json` | regras de alerta persistidas via `POST /v1/alerts/rules` |
| `.forge-lens/artifacts/` | criado pelo `init`, reservado a artefatos |
| `.forge-lens/install/receipt.json` | recibo de instalação (`install-status`) |

Resolução de config (maior prioridade primeiro): argumento explícito →
env `FORGE_LENS_*` → `.forge-lens/config.toml` → default. `data_dir` é
sempre `<cwd>/.forge-lens` — comandos CLI operam no projeto do diretório
corrente; `--db <path>` aponta para outro `lens.db` em qualquer comando
que o aceite.

## 2. Rodando a API (`forge-lens serve`)

```bash
forge-lens serve                          # bind vem de `endpoint` (default http://127.0.0.1:7777)
forge-lens serve --host 0.0.0.0 --port 8080
forge-lens serve --db /caminho/outro.db
forge-lens serve --ssl-certfile cert.pem --ssl-keyfile key.pem
```

| Flag | Efeito |
|---|---|
| `--host` | sobrescreve o host de `endpoint` |
| `--port` / `-p` | sobrescreve a porta de `endpoint`; `--port 0` = porta efêmera (uvicorn loga a real) |
| `--db` | `lens.db` alternativo |
| `--ssl-certfile` | certificado TLS — passado ao uvicorn; o banner muda para `https://` |
| `--ssl-keyfile` | chave privada TLS |

Sem `uvicorn` instalado: `uvicorn not installed. Run: uv sync --extra server`
→ exit 2. `endpoint` malformado → `serve failed: invalid endpoint ...` → exit 2.

### Bind fora de loopback

Ao subir em host não-loopback, `serve` emite aviso no stderr antes de
bindar — verificado em `_warn_if_exposed` (cli/main.py):

- Sem `api_key`: *"all trace data is readable and ingest is open to
  anyone on the network. Set FORGE_LENS_API_KEY or api_key in config.toml."*
- Com `api_key` mas `auth_reads=false` (default): *"api_key protects
  ingest only; GET /v1/traces, /v1/health and /v1/metrics stay
  unauthenticated by design (local-first reads). Set auth_reads=true to
  guard them."*

TLS aqui é passthrough do uvicorn — não há termination proxy embutido,
cert renewal nem HTTP→HTTPS redirect no código.

## 3. Modos de autenticação

`auth_mode` em config.toml ou `FORGE_LENS_AUTH_MODE`: `none` (default) |
`key` | `keys`. Modo efetivo (`api/app.py`): `auth_mode` explícito vence;
senão `api_key` configurada **implica** `key`; senão `none`.
`auth_mode = "key"` sem `api_key` → `create_app` levanta `ValueError` na
inicialização (todo write seria 401 permanente).

| Modo | Quem é o caller | O que exige header `X-Forge-Lens-Key` |
|---|---|---|
| `none` | `local`, role `admin` | nada — tudo aberto (single-user local) |
| `key` | `config-key`, role `admin` (a chave é `api_key` do config) | todos os writes (POST/PUT); reads só se `auth_reads = true` |
| `keys` | `key:<key_id>` — registro na tabela `api_keys` | **tudo**, reads inclusos — é o que torna tenancy por projeto enforçável |

Roles ordenados: `reader` ⊂ `writer` ⊂ `admin`.

- Writes (`POST /v1/traces`, `POST /v1/otlp/traces`, `PUT /v1/datasets*`):
  exigem `writer`+.
- Reads (`GET /v1/*`, `POST /v1/eval/*`, `/v1/alerts/evaluate`,
  `/v1/intel/*`, `/v1/analyze/*`): exigem `reader`+ apenas quando
  `auth_reads` ou `keys` estão ativos.
- Admin (`/v1/admin/*`, `POST|DELETE /v1/alerts/rules*`): exigem `admin`.
- `GET /v1/health` e `GET /v1/metrics` seguem a mesma regra de reads —
  **em `keys` mode um probe precisa de uma reader key** (trade-off
  documentado em `docs/cycles/cycle-06-12.md`). `/docs` e
  `/openapi.json` ficam sempre sem auth (decisão declarada).
- Escopo de projeto: uma key com `projects=["p1"]` só toca `p1` — reads
  e writes; omitir `project_id` resolve para o `project_id` configurado
  e passa pelo mesmo check. `projects` vazio = unscoped (todos).

### Mint, scope e revoke

**Bootstrap em `keys` mode é via CLI** — `forge-lens keys` escreve
direto no store, sem servidor e sem auth (o CLI é admin local por
definição). É o único caminho para a primeira admin key, já que
`POST /v1/admin/keys` exige admin.

```bash
# criar (secret impresso UMA vez — o store guarda só o SHA-256)
forge-lens keys create --name ci-ingest --role writer --project prod --project staging
forge-lens keys create --name reader-local --role reader          # sem --project = unscoped
forge-lens keys create --name admin --role admin
forge-lens keys list
forge-lens keys revoke <key_id>        # exit 1 se não existe/já revogada
```

Secrets têm formato `flk_sk_<urlsafe>`; key_ids `flk_<hex12>`.

**Via API** (admin; admin *scoped* só cria/revoga keys dentro do próprio
escopo — mintar unscoped ou projeto alheio → `403`):

```bash
curl -X POST http://127.0.0.1:7777/v1/admin/keys \
  -H "X-Forge-Lens-Key: $ADMIN_SECRET" -H "Content-Type: application/json" \
  -d '{"name": "ci", "role": "writer", "projects": ["prod"]}'
# → {"key_id","secret","role","projects","warning": "the secret is shown once..."}

curl http://127.0.0.1:7777/v1/admin/keys -H "X-Forge-Lens-Key: $ADMIN_SECRET"
curl -X DELETE http://127.0.0.1:7777/v1/admin/keys/<key_id> \
  -H "X-Forge-Lens-Key: $ADMIN_SECRET"
```

**Em `key` mode** (shared secret): trocar `api_key` em config.toml /
`FORGE_LENS_API_KEY` e reiniciar o `serve` — a credencial é o próprio
config, não há tabela.

### Trilha de auditoria

`GET /v1/admin/audit?action=&actor=&limit=0..500&offset=` — **admin
unscoped apenas** (scoped admin recebe 403). Registra: `auth`/`scope`
denied, `key.create`, `key.revoke`, `traces.prune`,
`traces.archive_prune`, `alert_rule.save`, `alert_rule.delete`. O
append é best-effort — falha de auditoria nunca derruba um request. O
CLI grava `actor="cli"` em prune/archive.

## 4. Retenção

Dados crescem até o operador agir — não há job de expurgo automático.
`retention_days` em config **só** fornece o cutoff default do
`forge-lens prune` (0 = desabilitado); `archive` sempre exige `--before`
explícito.

### `forge-lens prune` — deletar do hot store

```bash
forge-lens prune --days 30                      # traces iniciadas há mais de 30 dias
forge-lens prune --before 2025-01-01T00:00:00   # ISO datetime (naive = UTC)
forge-lens prune --project pesquisa --days 7
forge-lens prune --days 30 --yes                # -y pula a confirmação
```

- Critério: `started_ns < cutoff`, granularidade de **trace inteira**
  (spans morrem com a trace — nunca meia-trace).
- `--before` e `--days` são exclusivos (juntos → exit 2); `--days < 0`
  → exit 2; sem nenhum flag e `retention_days = 0` → `no cutoff` exit 2.
- Sem `--project`, a confirmação cobre **todos os projetos** (`*`).
- Sem `--yes`, pede confirmação interativa. Grava audit `traces.prune`.

### `forge-lens archive` — exportar antes de deletar

Archive-first: dados só saem do hot store **depois** de pousarem no
arquivo — nunca delete-before-export.

```bash
forge-lens archive --before 2025-01-01 --out archive-2024.ndjson
forge-lens archive --before 2025-06-01 --out arq.ndjson --append   # acrescenta
forge-lens archive --before 2025-01-01 --out arq.ndjson --delete --yes
```

- Exporta uma linha NDJSON por trace **encerrada** com `ended_at <
  cutoff`; traces sem `ended_at` nunca são arquivadas.
- `--out` existente sem `--append` → recusa truncar arquivo anterior
  (exit 2).
- `--delete` remove **exatamente os trace_ids exportados**
  (`delete_trace_ids`) — a garantia é "só o que foi exportado", não um
  critério re-computado. Pede `--yes` ou confirmação.
- Registra audit `traces.archive_prune` com o path do arquivo.

### Via API

`POST /v1/admin/prune` (admin) — body:

```json
{"older_than_days": 30, "project_id": "prod"}
{"before": "2025-01-01T00:00:00Z", "all_projects": true}
```

`before` e `older_than_days` são exclusivos; `older_than_days` deve ser
finito em 0..106000 (teto int64-ns). `all_projects: true` é opt-in
explícito e só passa para admin **unscoped** — sem ele, `project_id`
(ou o default configurado) passa por scope-check normal.

## 5. Backup e restore

O store é SQLite com `PRAGMA journal_mode=WAL`, `synchronous=NORMAL`,
`busy_timeout=5000` (sqlite.py). Não existe comando de backup/checkpoint
embutido — use a ferramenta `sqlite3` ou pare o processo:

```bash
# Online, consistente (usa a backup API do SQLite — lida com o WAL):
sqlite3 .forge-lens/lens.db ".backup 'lens-backup.db'"

# Alternativa: consolidar e copiar o arquivo único
sqlite3 .forge-lens/lens.db "PRAGMA wal_checkpoint(TRUNCATE);"
cp .forge-lens/lens.db lens-backup.db

# Alternativa fria: parar o serve/CLI e copiar — ao fechar a última
# conexão o SQLite faz checkpoint do WAL para dentro de lens.db
# (comportamento do SQLite, não código do Lens).
```

**Não copie só `lens.db` com o WAL cheio e servidor rodando** — dados
recentes vivem em `lens.db-wal`; `stats().db_size_bytes` já conta
lens.db + -wal + -shm. Se copiar a quente sem `.backup`, leve os três
arquivos juntos e ainda assim é a opção mais frágil das três.

**Restore** (servidor parado):

```bash
rm -f .forge-lens/lens.db-wal .forge-lens/lens.db-shm   # WAL velho ≠ main file novo
cp lens-backup.db .forge-lens/lens.db
forge-lens doctor    # esperado: [ok] sqlite store readable  schema=1.0.0
```

Deixar um `-wal` de outro arquivo ao lado de um `lens.db` restaurado pode
replay-ear frames velhos no banco novo — sempre remova os sidecars ao
restaurar um arquivo checkpointed.

## 6. Monitoramento

| Endpoint | Auth | Conteúdo |
|---|---|---|
| `GET /v1/health` | reader quando `auth_reads`/`keys` | `{status:"ok", version, storage:"sqlite", stats?}` — stats globais para key unscoped; key scoped a 1 projeto recebe stats daquele projeto; scoped a vários → sem stats. Cria o DB lazy se ainda não existe. |
| `GET /v1/metrics` | idem | `{counters, gauges, histograms}` in-process |
| `GET /v1/ops/summary?project_id=` | idem | stats + falhas recentes — janela limitada a 200 traces (declarado no payload) |
| `GET /v1/admin/audit` | admin unscoped | trilha de auditoria |

Contadores expostos (`core/metrics.py`): `lens.api.requests`,
`lens.api.errors`, `lens.ingest.spans`, `lens.ingest.traces`,
`lens.ingest.events`, `lens.ingest.duplicates`, `lens.ingest.rejected`,
`lens.ingest.errors`, `lens.sanitize.attrs_redacted`,
`lens.sanitize.attrs_truncated`; histogramas `lens.ingest.latency_ms`,
`lens.query.latency_ms` (janela de 1024 observações, min/max/avg).
São in-process — reiniciar zera; não há exportador OTLP das métricas do
próprio Lens.

`forge-lens doctor [path] [--db]` — health offline: `[ok]`/`[FAIL]` por
check (`python>=3.11`, `data dir exists`, `sqlite store readable` +
`schema=`, `server extras installed`, `sdk importable`,
`pricing file parseable`), `[info]` para não-fatal (db ainda não criado,
extras ausentes). Exit 0 se nada FAIL, senão 1.

## 7. Alert rules (threshold)

Arquivo: `.forge-lens/alert-rules.json` (lista JSON de regras;
`load_rules` tolera arquivo truncado → `[]`).

```bash
# salvar/atualizar (admin unscoped — regras são globais)
curl -X POST http://127.0.0.1:7777/v1/alerts/rules \
  -H "X-Forge-Lens-Key: $ADMIN_SECRET" -H "Content-Type: application/json" \
  -d '{"rule": {"rule_id": "err-rate", "metric": "error_rate", "op": ">",
        "threshold": 0.05, "severity": "critical", "message": "taxa de erro alta"}}'

# deletar
curl -X DELETE http://127.0.0.1:7777/v1/alerts/rules/err-rate \
  -H "X-Forge-Lens-Key: $ADMIN_SECRET"

# avaliar (reader em keys/auth_reads; aberto em none/key sem auth_reads)
curl -X POST http://127.0.0.1:7777/v1/alerts/evaluate \
  -H "Content-Type: application/json" \
  -d '{"use_saved": true, "project_id": "prod",
       "rules": [{"rule_id": "inline", "metric": "error_count", "op": ">=", "threshold": 1}]}'
```

- Métricas disponíveis: `span_count`, `trace_count`, `error_count`,
  `error_rate` — computadas do store na hora do request (escopo de
  `project_id`).
- Ops: `>`, `>=`, `<`, `<=`, `==`. Regra sem `rule_id` string ou
  `threshold` não-numérico é ignorada; métrica desconhecida nunca dispara.
- **Cap de 100**: `use_saved` mescla o arquivo às regras do body; total
  >100 → `422`. O save também enforça: arquivo já com 100 e rule_id novo
  → `422` (replace de um existente passa).
- `severity` default `"warning"`; `message` default é gerado do
  metric/op/threshold. Resultado: `{fired: [...], evaluated, project_id}`.

## 8. Tarefas administrativas comuns

| Tarefa | Comando |
|---|---|
| Inicializar projeto | `forge-lens init . --project <id>` |
| Verificar instalação/store | `forge-lens doctor [--db <path>]` — exit 1 em qualquer `[FAIL]` |
| Ingerir arquivo offline | `forge-lens ingest <file.json> [--format auto] [--project <id>]` — respeita `max_payload_bytes` como a API |
| Listar/inspecionar | `forge-lens traces list [--status error] [-n 50]` — filtros `--session`, `--tool`, `--q`, `--since`/`--until` (ISO), `--min-duration-ms`/`--max-duration-ms`; `forge-lens traces show <id>`; `forge-lens graph|cost|incident|eval <id>` |
| Roll-up de sessões | `GET /v1/sessions?project_id&limit&offset` — uma linha por `session_id` observada, mais recente primeiro (read_deps) |
| Importar sessão Claude Code | `forge-lens import-claude <session.jsonl> [--project claude-code]` |
| MCP stdio (read-only) | `forge-lens mcp-serve [--db]` — abre o store **readonly**; DB inexistente → `cannot open store`, `pricing_file` inválido → `cannot load pricing registry`; ambos exit 2 |
| Recibo de instalação | `forge-lens install-status [--data-dir .forge-lens]` — JSON `{status, doctor}` |
| Ciclar secret vazada | `keys` mode: `forge-lens keys revoke <key_id>`; `key` mode: novo `api_key` + restart |

## 9. Referência de config

`.forge-lens/config.toml` (seção `[lens]`) e env equivalente:

| Chave | Env | Default | Efeito |
|---|---|---|---|
| `project_id` | `FORGE_LENS_PROJECT` | `"default"` | projeto default |
| `db_path` | `FORGE_LENS_DB` | `<data_dir>/lens.db` | caminho do SQLite |
| `capture` | `FORGE_LENS_CAPTURE` | `"metadata"` | `metadata` (default seguro) \| `full` |
| `api_key` | `FORGE_LENS_API_KEY` | — | shared secret; implica modo `key` |
| `auth_reads` | `FORGE_LENS_AUTH_READS` | `false` | estende a chave aos GETs |
| `auth_mode` | `FORGE_LENS_AUTH_MODE` | `"none"` | `none` \| `key` \| `keys` |
| `retention_days` | `FORGE_LENS_RETENTION_DAYS` | `0` | cutoff default do `prune` (0=off) |
| `endpoint` | `FORGE_LENS_ENDPOINT` | `"http://127.0.0.1:7777"` | bind do `serve` **e** alvo dos SDKs |
| `pricing_file` | `FORGE_LENS_PRICING_FILE` | — | JSON de preços (erro de parse = falha no startup, não 500 por request) |
| `max_payload_bytes` | `FORGE_LENS_MAX_PAYLOAD_BYTES` | `8388608` | 413 acima |
| `max_traces_per_request` | `FORGE_LENS_MAX_TRACES_PER_REQUEST` | `10000` | 413 acima |
| `max_spans_per_request` | `FORGE_LENS_MAX_SPANS_PER_REQUEST` | `20000` | 413 acima |
| `max_events_per_span` | `FORGE_LENS_MAX_EVENTS_PER_SPAN` | `128` | trunca + flag `TRUNCATED` |
| `max_attr_value_chars` | `FORGE_LENS_MAX_ATTR_VALUE_CHARS` | `4096` | trunca com marcador |
| `rate_limit_per_minute` | `FORGE_LENS_RATE_LIMIT_PER_MINUTE` | `0` | 0=off; >0 → 429 no ingest |

Env malformada → `invalid env FORGE_LENS_*=…` no carregamento da config.
`config.toml` inválido → `invalid config file <path>: …`.
