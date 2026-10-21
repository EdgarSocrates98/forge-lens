# Guia — Troubleshooting

> Sintomas reais lidos do código (`api/app.py`, `cli/main.py`,
> `ingest/pipeline.py`, `storage/sqlite.py`, `mcp_server.py`,
> `apps/studio/`). Mensagens de erro citadas verbatim.

## Autenticação e escopo

| Sintoma | Causa | Fix |
|---|---|---|
| `401 invalid or missing API key` em POST | `key` mode (ou `api_key` setada): header `X-Forge-Lens-Key` ausente/errado | Enviar `X-Forge-Lens-Key: <api_key do config>` |
| `401` em GET (`/v1/traces`, `/v1/health`, `/v1/metrics`…) | `auth_reads = true` ou `auth_mode = "keys"` | Mesma chave no header; em `keys`, uma key **reader** basta para reads |
| `401` em *tudo*, inclusive `/v1/health` | `auth_mode = "keys"` — todo request exige key válida (não-revogada) | Mintar via `forge-lens keys create` (CLI escreve direto no store — é o bootstrap); checar `forge-lens keys list` |
| `403 writer role required` | key `reader` tentando POST ingest/datasets | Mintar/usar key `writer` ou `admin` |
| `403 role 'admin' required` | key não-admin em `/v1/admin/*` ou `/v1/alerts/rules` | Usar key `admin` |
| `403 project 'x' outside key scope` | key com `projects=[...]` tocando projeto fora do escopo — inclui *omitir* `project_id`, que resolve para o `project_id` do servidor | Passar `project_id` dentro do escopo ou usar key unscoped |
| `403 requested projects outside caller scope` | admin *scoped* mintando key unscoped ou de projeto alheio | Mintar só dentro do próprio escopo, ou usar admin unscoped |
| `403 audit trail requires an unscoped admin key` | `/v1/admin/audit` é restrito a admin unscoped | Usar key admin sem `projects` |
| `403 alert rules are global — an unscoped admin key is required` | salvar/deletar regras com key scoped | Idem — regras não são por-projeto |
| `404` onde se esperava `403` (ou vice-versa) | scope é checado **antes** da existência — key scoped nunca descobre se a trace existe em outro projeto | É proposital; valide escopo primeiro |

Em `none` mode, 401 é impossível: todo caller resolve como `local`/
`admin`. `auth_mode = "key"` sem `api_key` configurada faz o `create_app`
levantar `ValueError` no startup — o server nem sobe.

## Erros HTTP de ingestão

| Sintoma | Causa | Fix |
|---|---|---|
| `413 payload too large` | body > `max_payload_bytes` (default 8 MiB) — checado por Content-Length *e* por streaming real | Subir `max_payload_bytes` ou quebrar o batch |
| `413 N traces exceeds limit M` / `N spans exceeds limit M` | batch > `max_traces_per_request` (10000) / `max_spans_per_request` (20000) | Subir o limite ou splitar o envio |
| `415 OTLP/protobuf is not supported — POST application/json` | exporter OTLP configurado para protobuf | Apontar para OTLP/HTTP **JSON** |
| `400 invalid JSON: …` | body que não parseia (OTLP) ou JSON malformado em endpoints de body | Validar o payload localmente antes |
| `422` envelope `TraceIngest` inválido | contrato `schemas/v1/trace-ingest.json` violado | Conferir campos obrigatórios da Trace/Span |
| `422 unencodable string (lone surrogate)` | payload com surrogates UTF-16 soltos (WTF-8) — `json.loads` aceita `\ud800`, o SQLite não | Serializar UTF-8 limpo no produtor; remover surrogates |
| `422 … outside the int64 nanosecond range (~1678-2262)` | timestamp fora do range int64-ns — típico: ms enviado como ns, ou ano >2262 | Corrigir unidade/formato do timestamp no produtor |
| `422 … span(s) declare a different trace_id` | span dentro da Trace com `trace_id` divergente — checado no batch inteiro **antes** de qualquer escrita | Corrigir o agrupamento no produtor |
| `429` | `rate_limit_per_minute > 0` excedido (in-process, janela fixa de 60s) | Backoff no cliente; `0` desliga |
| `404 trace not found` | id inexistente **ou** `project_id` errado (lookup é por `(project_id, trace_id)`) | Checar `project_id` — o default é o `project_id` do config |
| `409 dataset version … exists` | versões de dataset são append-only | Nova `version` |

### Garantias de batch parcial (o que esperar)

- **Validação é all-or-nothing**: envelope nativo é validado por pydantic
  inteiro; depois `_ensure_storable` checa **cada** trace (project_id
  não-vazio, range int64-ns, strings encodáveis) **antes do primeiro
  upsert** — erro de input nunca deixa meio batch escrito.
- **Escrita é por-trace**: cada `upsert_trace` é uma transação — crash no
  meio nunca deixa meia-trace, mas pode deixar traces anteriores do batch
  commitadas. Retry é seguro: spans idênticos viram `duplicate_spans`
  (dedupe por `span_hash`), conteúdo alterado (ex.: `end_time` tardio)
  faz merge e conta como `updated_spans`.
- **OTLP é tolerante por design**: spans malformados vão para quarentena
  — motivo em `warnings[]`, os válidos entram. `warnings` é limitado a
  200 entradas + nota de overflow.
- `integrity_flags` nos spans (`missing_parent`, `missing_end`,
  `partial_trace`, `orphan`, `clock_skew`, `out_of_order`, `duplicate`,
  `conflict`, `truncated`, `unknown_tokens`) são vereditos do engine —
  flags declaradas pelo produtor são descartadas no ingest.

## CLI e operação

| Sintoma | Causa | Fix |
|---|---|---|
| `uvicorn not installed. Run: uv sync --extra server` → exit 2 | extra `server` ausente | `uv sync --extra server` (ou `pip install "forge-lens[server]"`) |
| `serve failed: invalid endpoint …` → exit 2 | `endpoint`/`FORGE_LENS_ENDPOINT` malformado | Corrigir para `http://host:port` |
| `warning: binding <host> with no api_key …` | bind não-loopback com ingest e reads abertos | Esperado — setar `api_key`/`auth_reads` ou voltar a loopback |
| `forge-lens ingest` → `exceeds max_payload_bytes` | arquivo maior que o limite (o CLI aplica o mesmo bound da API) | `--db`/`config` com limite maior, ou splitar o arquivo |
| `forge-lens ingest` → `unknown --format` → exit 2 | valor fora de `native\|otlp\|auto` | `--format auto` detecta OTLP por `resourceSpans` |
| `forge-lens prune` → `no cutoff` → exit 2 | nem `--before`, nem `--days`, e `retention_days = 0` | Passar um cutoff ou setar `retention_days` |
| `forge-lens archive` → `refusing to truncate a prior archive` → exit 2 | `--out` já existe | `--append` ou outro caminho |
| `forge-lens keys create` → `invalid role` → exit 2 | role fora de `reader\|writer\|admin` | Corrigir `--role` |
| `forge-lens keys revoke <id>` → exit 1 | key inexistente ou já revogada | `forge-lens keys list` para ver estado |
| `forge-lens mcp-serve` → `cannot open store at <path>` → exit 2 | DB não existe — `mcp-serve` abre **readonly** e nunca cria `lens.db` | Rodar um ingest/`keys create` antes, ou apontar `--db` para um `lens.db` real |
| `forge-lens mcp-serve` → `cannot load pricing registry: …` → exit 2 | `pricing_file` malformado — falha no startup, uma vez | Corrigir/remover `pricing_file` |
| `forge-lens traces list` → `invalid --since …` / `invalid --min-duration-ms …` → exit 2 | filtro de data não-ISO ou duração negativa/não-finita | ISO-8601 em `--since`/`--until`; ms finito ≥0 nas durações |
| `GET /v1/traces` → `422 status: one of ['error', 'ok', 'unset']` | `status=` fora do enum | Usar `error\|ok\|unset` |
| `GET /v1/traces` → `422 <campo>: unencodable string (lone surrogate)` | surrogate em query param (`session_id`/`q`/`tool`) | mesma correção do body — UTF-8 limpo |
| `GET /v1/traces` → `422 <campo>: non-negative finite number required` / `422 since|until: …` | `min_duration_ms`/`max_duration_ms` inválido ou data malformada | corrigir o param — validação roda antes de qualquer acesso ao store |
| `trace <id> not found` → exit 1 | id errado ou projeto errado | `--project` correto; `traces list` para conferir |
| `cost failed: invalid pricing_file` → exit 2 | `pricing_file` não parseia | Corrigir o JSON ou remover a chave do config |

### `forge-lens doctor` — lendo os FAIL

Exit 1 se **qualquer** `[FAIL]`; `[info]` não é falha.

| Linha | Significado | Ação |
|---|---|---|
| `[FAIL] python>=3.11` | interpretador antigo | Python ≥3.11 |
| `[FAIL] data dir exists` | `.forge-lens/` ausente no dir (ou `doctor` rodado fora do projeto) | `forge-lens init .`, ou `doctor <path>`/`--db` correto |
| `[FAIL] sqlite store readable` | DB corrompido/permissão — aberto readonly, sem mutar | Restaurar do backup (ver `operations-runbook.md` §5) |
| `[info] db not created yet` | normal antes do primeiro ingest | — |
| `[info] server extras missing` | `serve` indisponível | `uv sync --extra server` |
| `[FAIL] sdk importable` | pacote quebrado | reinstalar |
| `[FAIL] pricing file parseable` | `pricing_file` inválido — `serve` também falha no startup | corrigir/remover `pricing_file` |

## Store / SQLite

| Sintoma | Causa | Fix |
|---|---|---|
| `database is locked` | outro processo escrevendo no mesmo `lens.db`; `busy_timeout=5000` tolera picos, não lock permanente | Um writer por DB (`--db` distintos); matar processo zumbi segurando o WAL |
| `schema_version` reportada como `1.0.0` | `SCHEMA_VERSION = "1.0.0"` gravado em `lens_meta` a cada open; `init_schema` é `CREATE TABLE IF NOT EXISTS` | Não há runner de migração — a coluna é gancho para o futuro (docstring de sqlite.py). Abrir DB antigo só cria tabelas faltantes e regrava a versão |
| DB inchado mesmo após `prune` | delete libera páginas; o arquivo não encolhe sozinho | `sqlite3 lens.db "VACUUM;"` com o servidor parado (operação do SQLite — não há comando Lens para isso) |
| Restore misturou dados | `-wal`/`-shm` velhos ao lado de um `lens.db` restaurado → frames obsoletos replicados | Parar o servidor, apagar os sidecars, só então recolocar o arquivo |

## Studio (`apps/studio`)

| Sintoma | Causa | Fix |
|---|---|---|
| Lista vazia / erro de rede | `lens-api-base` errado — default é `http://localhost:7777/v1` e os paths (`/traces`, …) são concatenados nele | ⚙ do header (ou `localStorage['lens-api-base']`) apontando para `<scheme>://<host>:<port>/v1` |
| `HTTP <status> — set an API key (localStorage 'lens-api-key')` | request falhou — típico: 401/403 com server em `key`+`auth_reads` ou `keys` mode | ⚙ → API key, ou `localStorage['lens-api-key'] = '<secret>'` |
| Request cross-origin bloqueado (dev em `:4200`) | a API **não** configura CORS — browser bloqueia `localhost:4200 → localhost:7777` | `ng serve --proxy-config proxy.conf.json` (o arquivo existe em `apps/studio/` e encaminha `/v1` → `:7777`, mas **não** está ligado no `angular.json` — a flag é obrigatória). Em produção: mesma origem ou reverse proxy |
| Key no localStorage | o app guarda o secret em `localStorage` (README do Studio declara: legível por qualquer script da página) | Aceitável em loopback local; não reuse uma key valiosa se servir o Studio de origem compartilhada |

## Comportamentos que parecem bug mas são design

| Observação | Por quê |
|---|---|
| `GET /v1/health` criou `.forge-lens/lens.db` | store/pipeline são lazy — o primeiro request que toca o store cria diretório e tabelas (`importers ≠ servers`, mas requests ≠ importers) |
| Reads abertas em `key` mode | `auth_reads` default `false` — local-first mantém GETs abertos por design; ative `auth_reads` para fechar |
| `updated_spans` > 0 em re-ingestão | re-entrega com conteúdo **alterado** faz merge (ex.: `end_time` tardio completa um span parcial) — não é duplicata |
| Flags do produtor sumiram | `integrity_flags` enviados pelo cliente são zerados no ingest e recomputados — veredito é do engine |
| `/docs` e `/openapi.json` sem auth em `keys` mode | decisão declarada (schema-only, necessário p/ tooling) — residual em `docs/cycles/cycle-06-12.md` |
| `lens.db` não encolhe | WAL + delete ≠ shrink; ver `VACUUM` acima |
