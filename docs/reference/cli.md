# Referência — CLI `forge-lens`

```bash
forge-lens --help          # ajuda geral
forge-lens --version
```

## `forge-lens init [path] --project <id>`

Cria `.forge-lens/` com `config.toml` e `artifacts/` no diretório.
Idempotente: re-init preserva um `config.toml` existente — um
`--project` diferente não sobrescreve o project_id já configurado.

## `forge-lens serve [--host 127.0.0.1] [--port 7777] [--db <path>] [--ssl-certfile F] [--ssl-keyfile F]`

Sobe a API (FastAPI+uvicorn). Requer o extra `server`
(`uv sync --extra server`); sem ele → exit 2. Endpoints em
`docs/reference/api.md`. `--port` aceita 0–65535 (0 = porta efêmera);
fora da faixa → exit 2 antes do bind. Bind fora de loopback sem
`api_key` configurada emite um aviso no stderr — leituras de
`/v1/traces*` são abertas por design local.

## `forge-lens ingest <file> [--db] [--project] [--format native|otlp|auto]`

Ingere um JSON local (envelope `TraceIngest` ou `ExportTraceServiceRequest`
OTLP) direto no store — sem servidor. `--format auto` detecta OTLP pela
presença de `resourceSpans`; valor fora de `native|otlp|auto` → exit 2.
Import de arquivo marca as traces `provenance=imported` (vs `observed`
da ingestão viva) — `--format native` honra a provenance do envelope.

## `forge-lens traces list [--db] [--project] [--status ok|error|unset] [--session S] [--tool T] [--q S] [--model M] [--agent A] [--host H] [--min-tokens N] [--since ISO] [--until ISO] [--min-duration-ms N] [--max-duration-ms N] [--limit N] [--offset N] [--json]`

Lista traces mais recentes com status, span_count e error_count.
`--tool`/`--q`/`--model`/`--agent`/`--host` são substring (`--model` casa
`gen_ai.request|response.model`; `--agent` casa `gen_ai.agent.name` ou a
identidade do produtor; `--host` casa `source.host`/`host.name`);
`--min-tokens` exige soma de `gen_ai.usage.total_tokens` ≥ N (inteiro
`0..2^63−1`; traces sem observação de uso nunca casam, inclusive em
`N=0`); `--since`/`--until` limitam `started_at`; filtros de duração
excluem traces sem `ended_at`. `--limit`/`--offset`/`--min-tokens`/
`--*-duration-ms` fora do int64 ou negativos → exit 2, nunca um bind
no driver. `--offset` pula N linhas (paginação sobre a ordenação
"mais recente primeiro").

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

## `forge-lens eval <trace_id> [--db] [--project] [--budget-ms N] [--evaluator NAME]... [--params-json S] [--json]`

Bateria de avaliadores determinísticos (Cycle 06): `error_free` +
`tool_success` sempre; `latency_budget` quando `--budget-ms` é passado.
`--evaluator` é repetível e aceita qualquer nome do registry
(`forge_lens.eval.evaluators.EVALUATORS`) — nome desconhecido → exit 2
com a lista de válidos. `--params-json` é um objeto JSON de params
compartilhado por todos os specs `--evaluator` — não-objeto ou JSON
malformado → exit 2. Score `n/a` + motivo quando a evidência é
insuficiente — nunca inventa veredito.

## `forge-lens failure|loops|efficiency|recommendations|security|replay|correlate <trace_id> [--db] [--project] [--json]`

Análises por trace — paridade com os GETs `/v1/traces/{id}/…`:

- `failure`: classificação determinística da falha (categoria +
  evidência de spans).
- `loops`: findings heurísticos de loop/sem-progresso; `--repeat-threshold N`
  (1..50, padrão 3) espelha o `repeat_threshold` da API.
- `efficiency`: roll-up de eficiência (tools/llm/errors/retries/tokens).
- `recommendations`: recomendações `candidate` com evidência — ganhos
  nunca são reivindicados sem validação experimental.
- `security`: observações de segurança MCP/tool-call (modo observer —
  só evidência, nunca bloqueio).
- `replay`: timeline da execução gravada (span_start/event/span_end com
  offset em ms).
- `correlate`: sinais de co-ocorrência temporal dentro da trace
  (retries→erros, troca de modelo, crescimento de contexto) —
  associação descritiva, nunca causal.

Trace inexistente → mensagem limpa + exit 2. `--json` emite o mesmo
contrato da rota correspondente.

## `forge-lens analyze --list` · `forge-lens analyze <specialist> <trace_id> [--db] [--project] [--json]`

Executa um especialista registrado (Cycle 07) sobre uma trace —
paridade com `POST /v1/analyze/{specialist}/{trace_id}`, falando
direto com o store. `--list` mostra os 11 especialistas com descrição
curta. Especialista desconhecido → exit 2 com a lista de nomes
válidos; faltar `<specialist>` ou `<trace_id>` → exit 2. O registry de
preços é injetado no contexto como na API (para `cost_analyst`).
`--json` emite o `AgentRun` serializado (`run_id`, `specialist`,
`stop_reason`, `iterations`, `tokens_observed`, `cancelled`,
`findings`).

## `forge-lens eval-suggest <trace_id>... [--db] [--project] [--json]`

Sugestões `candidate` de avaliadores/casos de dataset a partir da
evidência de falha de uma ou mais traces (Cycle 12 §12.7): classifica
cada trace e chama o agregador uma vez para o conjunto — a mesma
categoria de falha em ≥2 traces distintas marca `evidence.recurring`.
As sugestões saem agrupadas por nome; nada é aplicado sozinho e
datasets nunca são modificados. Trace inexistente → exit 2. `--json`
emite `{"suggestions": [...], "note": ...}`.

## `forge-lens doctor [path] [--db]`

Verifica Python ≥3.11, diretório de dados, store legível (schema_version,
contagens), extras de servidor e import do SDK. Exit 0/1.

## `forge-lens sessions [--db] [--project] [--limit N] [--json]`

Roll-up de sessões (espelha `GET /v1/sessions`): session_id,
traces, spans e última atividade — ordenado por atividade recente.

## `forge-lens tools [--db] [--project] [--limit N] [--json]`

Roll-up de ferramentas (espelha `GET /v1/tools`): uma linha por
`tool.name` com calls, traces, errors e duração média — spans tool sem
o atributo caem em `(unnamed)`.

## `forge-lens capabilities [--json]`

Matriz honesta de capacidades por host (espelha `GET /v1/capabilities`
e `lens.capabilities` no MCP): mecanismo, adapter, cobertura e limites
— `unsupported` é resposta válida. Local, não abre o store.

## `forge-lens stats [--db] [--project] [--json]`

Resumo operacional (espelha `GET /v1/ops/summary`): stats do store
(traces/spans/error_spans/schema/db_size) + falhas recentes — janela
limitada de 200 traces, não um total da frota.

## `forge-lens intel efficiency [--db] [--project] [--session S] [--tool T] [--limit N] [--json]`

Roll-up de eficiência da frota (espelha `POST /v1/intel/efficiency`)
sobre uma janela limitada de traces recentes (`--limit` clama 0..500),
opcionalmente filtrada por `--session`/`--tool`. Métricas sem sinal
saem `null` — ausência de evidência, não zero.

## `forge-lens intel correlate [--db] [--project] [--limit N] [--json]`

Tabelas de co-ocorrência da frota (espelha `POST /v1/intel/correlate`)
sobre janela limitada — associação descritiva, nunca causal; coortes
pequenas são rotuladas `insufficient_data`.

## `forge-lens intel regress --a-json <arr> --b-json <arr> [--db] [--project] [--json]`

Regressão entre duas listas de trace_ids (espelha
`POST /v1/intel/regress`): 1..500 strings por lado, deltas observados
— sem teste de significância (`eval compare` detém a estatística).
IDs inexistentes são tolerados e reportados em `missing_trace_ids`;
JSON malformado ou shape inválido → exit 2.

## `forge-lens datasets list [--db] [--project] [--json]` · `forge-lens datasets show <id> [--db] [--project] [--json]`

Leitura de datasets de eval (espelha `GET /v1/datasets` +
`/v1/datasets/{id}` + `/versions`): `list` resume id/nome/task/created;
`show` traz metadata + versões append-only com contagem de casos.
Dataset inexistente → exit 2.

## `forge-lens experiments create <id> --evaluators-json <arr> --cohorts-json <obj> [--name S] [--description S] [--dataset-id S] [--dataset-version S] [--metadata-json S] [--db] [--project] [--json]`

Cria/substitui o spec de um experimento A/B (Cycle 13 — espelha
`PUT /v1/experiments/{id}`). O spec fixa cohorts + avaliadores para
cada `run` ser reproduzível; resultados são append-only.
`--evaluators-json`: array não-vazio (≤100) de objetos com `"name"`
string. `--cohorts-json`: objeto com `a` e `b`, cada um
`{"trace_ids": [1..500 strings], "label"?, "config"?}`.
`--dataset-version` exige `--dataset-id`; NaN/Infinity no spec →
exit 2. JSON malformado ou shape inválido → exit 2 limpo.

## `forge-lens experiments list [--db] [--project] [--limit N] [--offset N] [--json]`

Lista specs de experimentos (espelha `GET /v1/experiments`): id, nome,
status e created_at — `--limit` clama 1..500 como na API.

## `forge-lens experiments show <id> [--db] [--project] [--limit N] [--offset N] [--json]`

Spec + histórico append-only de runs (espelha
`GET /v1/experiments/{id}`); `--limit`/`--offset` paginam os runs.
Experimento inexistente → exit 2.

## `forge-lens experiments run <id> [--gate-json S] [--db] [--project] [--json]`

Executa o spec pelo mesmo caminho da API
(`POST /v1/experiments/{id}/run`): resolve as cohorts no projeto →
`evaluate_traces` em cada lado → `compare_runs` → gate opcional via
`--gate-json` (`eval.compare.apply_gate`) → persiste o relatório em
`experiment_results` com `run_id` e `run_at`. Experimento inexistente,
trace de cohort ausente ou spec de gate inválido → exit 2.

## `forge-lens keys create --name <n> [--role reader|writer|admin] [--project P]... [--db]`
## `forge-lens keys list [--db]` · `forge-lens keys revoke <key_id> [--db]`

Ciclo de vida de API keys (Cycle 08): o segredo aparece **uma vez** —
só o hash fica no store. `--project` repetível define o escopo de
tenancy; omitir = chave global.

## `forge-lens audit [--db] [--action S] [--actor S] [--limit N] [--offset N] [--json]`

Trilha de auditoria (espelha `GET /v1/admin/audit`): eventos só
existem quando auth esteve ativa — trilha vazia é estado normal, não
erro. Eventos não carregam atribuição de projeto, por isso não há
`--project` (mesmo na API). `--limit` 0..500 e `--offset` ≥ 0 dentro
do int64; fora → exit 2.

## `forge-lens alerts list [--json]` · `forge-lens alerts save --rule-json <obj> [--json]` · `forge-lens alerts delete <rule_id> [--json]` · `forge-lens alerts evaluate [--db] [--project] [--limit N] [--json]`

Regras de alerta por threshold (Cycle 07 §07.5) persistidas em
`<data-dir>/alert-rules.json` — arquivo local com escrita atômica e
ordenação por `rule_id`; `list`/`save`/`delete` não abrem o store.
`save` insere/substitui exigindo `rule_id` string e respeita o cap de
100 regras salvas (o mesmo que a API impõe na avaliação); `delete` de
regra inexistente → exit 2; arquivo de regras ausente ou corrompido =
zero regras. `evaluate` avalia as regras salvas contra métricas
derivadas do store — contadores agregados do projeto mais uma janela
limitada de traces recentes para métricas de duração/custo/tokens/loop
(`--limit` clama 0..500) — e emite `{"fired": [...], "evaluated": N}`.
JSON malformado ou shape inválido → exit 2.

## `forge-lens prune [--db] [--project P] [--before ISO | --days N] --yes`

Expurgo por idade (Cycle 08). Sem `--before`/`--days` usa
`retention_days` da config; uma das duas fontes é obrigatória.
`--yes` confirma a deleção. Sem critério de cutoff → exit 2; recusar
a confirmação interativa → exit 1 (`Aborted.`).

## `forge-lens archive --before <ISO> --out <file> [--db] [--project] [--delete --yes] [--append]`

Arquivamento frio (Cycle 08): exporta traces encerradas antes do
cutoff em NDJSON (uma linha por trace; `--before` e `--out` são
obrigatórios). Sem `--project`, exporta **todos** os projetos.
`--delete` apaga após exportar — só o que foi exportado. Sem
`--append`, recusa sobrescrever um arquivo existente (exit 2).

## `forge-lens import-claude <session.jsonl> [--db] [--project claude-code]`

Importa uma sessão Claude Code (JSONL `assistant`/`user` com
`tool_use`/`tool_result` aninhados) como trace derivada de log. Arquivo
limitado a 256 MiB; eventos inválidos → exit 2, zero linhas persistidas.

## `forge-lens import-otel <file> [--db] [--project P]`

Importa um dump OTLP/HTTP-JSON — paridade com `POST /v1/otlp/traces`:
aceita **somente** `ExportTraceServiceRequest` como objeto JSON
(protobuf não é decodificável aqui). Arquivo limitado por
`max_payload_bytes`; JSON malformado / não-objeto / arquivo ausente →
exit 2. Spans malformados são colocados em quarentena e reportados como
warnings sem rejeitar o lote; import de arquivo marca
`provenance=imported`. O fluxo passa pelo `IngestPipeline` (validação,
sanitize, dedupe) como `ingest`.

## `forge-lens mcp-serve [--db]`

Servidor MCP stdio read-only (Cycle 10): abre o store readonly, exit 2
se o DB não existir ou `pricing_file` for inválido. Tools em
`docs/reference/mcp.md`.

## `forge-lens install-status [--data-dir <path>]`

Lê o receipt de instalação (`install/receipt.json`) sem criar
diretórios — operação read-only.

## `forge-lens install`

Ciclo de vida do receipt de instalação (auditável, local-first). O
instalador portátil real é `install.sh`/`install.ps1` na raiz do repo —
estes verbos gerenciam o receipt em `<data-dir>/install/`:

```bash
forge-lens install [--profile minimal|recommended|full]
                   [--source pypi|wheel|git|zip|local|gitless|offline]
                   [--file <managed-file>]... [--dry-run]
                   [--data-dir <path>]
forge-lens install status    [--data-dir <path>]   # receipt lido
forge-lens install doctor    [--data-dir <path>]   # health; exit 1 se falha
forge-lens install uninstall [--data-dir <path>]   # remove arquivos rastreados
```

`install` sem subcomando grava o receipt (`--dry-run` só imprime);
`uninstall` remove apenas arquivos dentro do data-dir contidos no
receipt (containment por path resolvido) e deixa um receipt de
`uninstall` — a remoção é provável, não silenciosa.

Escopo das flags: `--data-dir` no grupo liga o verbo `install` sem
subcomando; `status`/`doctor`/`uninstall` ligam o próprio `--data-dir`
— passe a flag depois do verbo (`forge-lens install status --data-dir
X`), não antes.

## Instalador portátil (`install.sh` / `install.ps1`)

`./install.sh` (POSIX/Git Bash), `.\install.ps1` (PowerShell),
`bash install.windows.sh` (Git Bash) e `setup.sh`/`setup.ps1` delegam a
`scripts/forge_bootstrap.py` — pipeline `discover → resolve-python →
create-venv → install-runtime → write-launcher → smoke-verify →
register → report` lendo `forge.json`. Instala em
`~/.forge/installs/forge-lens`, launcher em `~/.local/bin` (ou shim
`.cmd`), variáveis `FORGE_LENS_*` em
`${XDG_CONFIG_HOME:-~/.config}/forjas/env.sh` (POSIX) ou HKCU (Windows).
Flags: `--dry-run`, `--editable`, `--install-dir`, `--no-launcher`,
`--python`, `--extras`. Detalhes em `docs/installation/standalone.md`.

## Homologação

Contrato verificado em `tests/unit/test_cli_homolog.py` (CliRunner, sem
subprocess e sem bind de porta). Códigos de saída:

| exit | significado |
| --- | --- |
| 0 | sucesso — inclusive resultados vazios (`no traces stored`, `pruned 0 trace(s)`, JSON `[]`) |
| 1 | falha operacional genérica ou confirmação abortada (`Aborted.`) |
| 2 | entrada inválida ou recurso inexistente — usage error do typer, flag fora de faixa, config/env malformados, `--db` inabível, trace/dataset não encontrado |

Regras uniformes verificadas por sweep parametrizado:

- Toda mensagem de erro vai para **stderr** (`err=True`); stdout fica
  limpo em falha — nunca `Traceback` em erro de usuário.
- Trace inexistente → `trace <id> not found` no stderr + exit 2 em
  **todos** os comandos com `<trace_id>` (`traces show`, `graph`,
  `cost`, `incident`, `eval`, `failure`, `loops`, `efficiency`,
  `recommendations`, `correlate`, `security`, `replay`, `analyze`,
  `eval-suggest`) — uniformizado neste ciclo; antes os cinco primeiros
  retornavam 1. Em `eval-suggest` qualquer id ausente aborta o conjunto
  com exit 2; em `experiments run`/`intel regress` um trace_id de
  cohort inexistente vira exit 2 ou `missing_trace_ids`
  respectivamente — paridade com as rotas espelhadas.
- `--db` apontando para diretório, arquivo não-sqlite ou destino sem
  permissão → `cannot open store at <path>` + exit 2 em todos os
  comandos de store (mesma mensagem que `mcp-serve` emite). `--db` com
  path inexistente **cria** um store novo — local-first por design.
- `FORGE_LENS_*` ou `config.toml` malformado → `invalid configuration:`
  + exit 2 nos comandos; no `doctor` vira `[FAIL] config parseable`
  + exit 1 (o health check reporta, não quebra).
- `--json` emite JSON parseável no stdout em todos os comandos de
  leitura/análise que o aceitam; `install status|doctor|uninstall`,
  `install` (apply) e `install-status` já emitem JSON por padrão.

Assimetrias registradas (comportamento atual, não alterado):

- `keys revoke` → exit 1 para "not found **ou** already revoked" — o
  store não distingue os dois casos (single bool).
- `import-claude` com arquivo legível mas sem eventos utilizáveis →
  exit 1 ("nothing imported"); `ingest` agrupa todo erro de
  arquivo/payload sob `ingest failed:` + exit 1, enquanto
  `import-otel` separa entrada inválida (2) de falha de pipeline (1).
- `sessions`/`tools` clamam `--limit` a 0..500 (paridade com a API);
  `traces list --limit` rejeita negativos (exit 2) e `--limit 0` lista
  vazio — divergência deliberada, documentada.
- Grupo sem subcomando (`forge-lens traces`, `keys`, `datasets`, raiz)
  imprime help e sai **2** (comportamento `no_args_is_help`).
- `install apply` não existe — `install` sem subcomando já é o apply.
- `mcp-serve` fala JSON-RPC em stdout; erros vão para stderr via
  `print` (correto para um servidor stdio — stdout é o canal do
  protocolo).
- `eval --budget-ms` inválido (negativo/NaN) não falha: o avaliador
  `latency_budget` reporta `n/a` com motivo — evidência, não crash.
- `audit` não tem `--project` (eventos de auditoria não carregam
  atribuição de projeto — paridade com `GET /v1/admin/audit`);
  `alerts list|save|delete` operam sobre `<data-dir>/alert-rules.json`
  sem abrir o store, portanto não aceitam `--db`/`--project` — só
  `alerts evaluate` os aceita.
- `--limit`: `audit`/`traces list` rejeitam valores fora de faixa
  (exit 2), enquanto `sessions`/`tools`/`experiments list`/`intel`
  clamam a 0..500 — mesma divergência deliberada já registrada entre
  `traces list` e os roll-ups.

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
