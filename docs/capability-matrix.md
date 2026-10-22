# Matriz de Capacidades — paridade de superfícies

Fonte única de verdade para "o que existe onde" no Forge Lens. Gerada
por auditoria mecânica do código (branch `cycle-13.1/hardening-homologation`)
— cada célula cita o arquivo que prova a afirmação. Nada é marcado como
suportado sem evidência de código; ausências são declaradas, não
escondidas.

Superfícies auditadas:

| Símbolo | Superfície | Fonte |
|---|---|---|
| Core | módulo do engine (sem rede, determinístico) | `src/forge_lens/` |
| HTTP | API FastAPI `/v1` | `src/forge_lens/api/app.py` |
| CLI | `forge-lens` (Typer) | `src/forge_lens/cli/main.py` |
| SDK-Py | `LensClient`/`LensTracer`/`HTTPExporter` | `src/forge_lens/sdk/` |
| SDK-TS | `@forge-lens/sdk` (`LensClient`/`LensTracer`) | `packages/forge-lens-sdk-ts/src/` |
| MCP | servidor stdio observer (`mcp-serve`) | `src/forge_lens/mcp_server.py` |
| Studio | Angular UI | `apps/studio/src/app/` (`api.ts`, `app.routes.ts`) |

Legenda:

- **IMPL** — implementado e coberto (com ponteiro de código).
- **PARCIAL** — parte da capacidade existe; a lacuna está nomeada.
- **AUSENTE** — nenhum código nessa superfície para a capacidade.
- **N/A** — não aplicável por design (ex.: MCP é read-only; Studio não
  ingere). Sempre com justificativa.

## 1. Matriz por área de capacidade

| Capacidade | Core | HTTP | CLI | SDK-Py | SDK-TS | MCP | Studio |
|---|---|---|---|---|---|---|---|
| **Ingest nativa** (`TraceIngest` → pipeline → store) | IMPL `ingest/pipeline.py`, `contracts/v1/trace.py` | IMPL `POST /v1/traces` (writer) | IMPL `ingest` (`main.py:220`) | IMPL `HTTPExporter` (`sdk/exporter.py:60`) | IMPL `HTTPExporter` (`tracer.ts:74`) | N/A — observer read-only por design | N/A — UI não é produtora |
| **Traces** (listar/filtrar/detalhar) | IMPL `storage/sqlite.py:list_traces:900`, `get_trace:858` | IMPL `GET /v1/traces`, `GET /v1/traces/{id}` | IMPL `traces list`, `traces show` (`main.py:307,441`) | IMPL `list_traces`, `get_trace` (`sdk/query.py:203,253`) | IMPL `listTraces`, `getTrace` (`query.ts:299,321`) | IMPL `lens.list_traces`, `lens.get_trace` | IMPL rotas `/traces`, `/traces/:id` |
| **Sessions** (roll-up) | IMPL `list_sessions` (`sqlite.py:1092`) | IMPL `GET /v1/sessions` | IMPL `sessions` (`main.py:534`) | IMPL `list_sessions` | IMPL `listSessions` | IMPL `lens.sessions` | IMPL rota `/sessions` (`sessions.ts`) |
| **Tools** (roll-up frota) | IMPL `list_tools` (`sqlite.py:1146`) | IMPL `GET /v1/tools` | IMPL `tools` (`main.py:564`) | IMPL `list_tools` | IMPL `listTools` | IMPL `lens.tools` | AUSENTE — sem página/chamada em `api.ts` |
| **Graph** (trace/session graph, critical-path, bottlenecks, analysis) | IMPL `graph/builder.py`, `graph/algorithms.py`, `intel/graph_intel.py` | IMPL 5 rotas `/v1/graph/*` | PARCIAL `graph <id>` cobre graph+critical-path+self-time (`main.py:502`); sem session-graph nem `/analysis` | IMPL `trace_graph`, `trace_critical_path`, `trace_bottlenecks`, `trace_graph_analysis`, `session_graph` | IMPL mesmos 5 métodos | AUSENTE — sem tool `lens.graph*` | IMPL rota `/graph` (`graph.ts`) + `getGraphAnalysis` no detalhe |
| **Cost** (custo derivado + waste + pricing) | IMPL `intel/cost.py`, `intel/pricing.py`, `intel/waste.py` | IMPL `GET /v1/traces/{id}/cost`, `GET /v1/analytics/pricing` | PARCIAL `cost` (`main.py:662`); sem comando de tabela de pricing | IMPL `trace_cost`, `pricing` | IMPL `traceCost`, `pricing` | IMPL `lens.cost` (mesmo payload: report + `pricing_source` + `waste[]`) | PARCIAL `getCost` no detalhe da trace; sem página de pricing |
| **Failure** (classificação + hipóteses) | IMPL `investigate/classify.py`, `investigate/hypothesis.py` | IMPL `GET /v1/traces/{id}/failure` (+ `/incident` embute hipóteses) | IMPL `failure` (`main.py:790`) | IMPL `trace_failure` | IMPL `traceFailure` | IMPL `lens.failure` | IMPL `getFailure` no detalhe |
| **Replay / incident bundle / stream** | IMPL `investigate/replay.py`, `investigate/bundle.py` | IMPL `GET …/replay`, `…/incident`, `…/stream` (SSE) | PARCIAL `replay`, `incident` (`main.py:963,715`); sem stream | PARCIAL `trace_replay`, `trace_incident`; `stream` excluído de propósito (SSE) | PARCIAL `traceReplay`, `traceIncident`; `stream` excluído de propósito | AUSENTE | PARCIAL `getReplay`; sem incident/stream |
| **Loops** (heurístico) | IMPL `ops/loops.py` | IMPL `GET /v1/traces/{id}/loops` | IMPL `loops` (`main.py:813`) | IMPL `trace_loops` | IMPL `traceLoops` | IMPL `lens.detect_loops` | IMPL `getLoops` no detalhe |
| **Correlation** (temporal, não-causal) | IMPL `intel/correlation.py` | IMPL `GET …/correlate` + `POST /v1/intel/correlate` | PARCIAL `correlate <id>` só por trace (`main.py:904`); sem frota | IMPL `trace_correlate` + `fleet_correlate` | IMPL `traceCorrelate` + `fleetCorrelate` | PARCIAL `lens.correlate` só por trace | AUSENTE |
| **Efficiency** | IMPL `intel/efficiency.py` | IMPL `GET …/efficiency` + `POST /v1/intel/efficiency` | PARCIAL `efficiency <id>` só por trace (`main.py:846`) | IMPL `trace_efficiency` + `fleet_efficiency` | IMPL `traceEfficiency` + `fleetEfficiency` | PARCIAL `lens.trace_efficiency` só por trace | AUSENTE |
| **Recommendations** | IMPL `intel/recommendations.py` | IMPL `GET …/recommendations` | IMPL `recommendations` (`main.py:876`) | IMPL `trace_recommendations` | IMPL `traceRecommendations` | AUSENTE | AUSENTE |
| **Eval** (bateria, run, compare+gate) | IMPL `eval/runner.py`, `eval/evaluators.py`, `eval/compare.py` | IMPL `GET …/eval`, `POST /v1/eval/run`, `POST /v1/eval/compare` | PARCIAL `eval <id>` bateria default + `latency_budget` (`main.py:761`); sem `eval run`/`compare` | IMPL `trace_eval`, `eval_run`, `eval_compare` | IMPL `traceEval`, `evalRun`, `evalCompare` | AUSENTE | IMPL `getTraceEval`, `runEval`/`runEvalSet`, `runCompare` |
| **Datasets** | IMPL `contracts/v1/dataset.py` + store (`sqlite.py:1237-1490`) | IMPL 6 rotas `/v1/datasets*` (PUT=writer, GET=reader) | PARCIAL `datasets list`/`show` read-only (`main.py:1119,1146`); sem escrita | PARCIAL só leitura (`list_datasets`, `get_dataset`, `list_dataset_versions`, `get_dataset_version`); sem `put_dataset*` | PARCIAL idem SDK-Py | PARCIAL `lens.list_datasets` só lista | IMPL `datasets.ts` + `putDataset`, `putDatasetVersion` |
| **Experiments** | IMPL `contracts/v1/experiment.py` + store (`sqlite.py:1494-1700`) | IMPL `PUT /v1/experiments/{id}`, `GET` lista/detalhe, `POST …/run` (PUT+run=writer) | AUSENTE — nenhum comando `experiments` | AUSENTE — nenhum método (lacuna real; ver divergências) | AUSENTE — idem | AUSENTE | IMPL `experiments.ts` + `listExperiments`, `getExperiment`, `putExperiment`, `runExperiment` |
| **Alerts** (regras de threshold) | IMPL `ops/alerts.py` (`evaluate_rules`, `resolve_metrics`, `load/save/delete_rule`) | IMPL `POST /v1/alerts/evaluate` (reader), `POST/DELETE /v1/alerts/rules*` (admin unscoped) | AUSENTE — nenhum comando de alerta | PARCIAL `alerts_evaluate`; sem escrita de regras | PARCIAL `alertsEvaluate`; idem | AUSENTE | PARCIAL `evalAlerts({use_saved:true})` em `ops.ts`; sem editor de regras |
| **Ops** (summary, stats, health, metrics) | IMPL `store.stats` (`sqlite.py:1194`) + `core/metrics.py` | IMPL `GET /v1/ops/summary`, `/v1/health`, `/v1/metrics` | IMPL `stats` (= ops/summary), `doctor` (saúde local) (`main.py:617,993`) | IMPL `ops_summary`, `health`, `metrics` | IMPL `opsSummary`, `health`, `metrics` | PARCIAL `lens.stats` = `store.stats` puro (sem janela/`recent_failures` do summary) | PARCIAL `getOps`; sem health/metrics |
| **Specialists** (`analyze`) | IMPL `ops/agents.py` (`SPECIALISTS`, `run_specialist`, `AgentBudget`) | IMPL `GET /v1/analyze/specialists`, `POST /v1/analyze/{s}/{id}` | AUSENTE | IMPL `list_specialists`, `run_analysis` | IMPL `listSpecialists`, `runAnalysis` | AUSENTE | AUSENTE |
| **Security** (observações MCP/tool-call, observer) | IMPL `security/mcp_inspect.py` | IMPL `GET /v1/traces/{id}/security` | IMPL `security` (`main.py:933`) | IMPL `trace_security` | IMPL `traceSecurity` | AUSENTE | AUSENTE |
| **Capabilities** (matriz de hosts) | IMPL `adapters/capabilities.py` | IMPL `GET /v1/capabilities` | IMPL `capabilities` (`main.py:594`) | IMPL `capabilities` | IMPL `capabilities` | IMPL `lens.capabilities` | IMPL rota `/capabilities` |
| **Admin** (keys, audit, prune, archive) | IMPL `security/keys.py` + store (`create_api_key:1707`, `list_audit:1831`, `prune_traces:1871`) | IMPL `POST/GET/DELETE /v1/admin/keys*`, `GET /v1/admin/audit`, `POST /v1/admin/prune` (admin) | PARCIAL `keys create|list|revoke`, `prune`, `archive` (`main.py:1057-1330`); sem comando `audit` | AUSENTE — exclusão documentada (`sdk/query.py:19-21`) | AUSENTE — idem (`query.ts:20-22`) | AUSENTE — read-only por design | AUSENTE |
| **MCP** (o próprio servidor observer) | IMPL `mcp_server.py` (`serve`, 12 tools) | N/A — protocolo stdio JSON-RPC, não HTTP | IMPL `mcp-serve` (`main.py:1341`) | N/A — SDK é cliente HTTP | N/A — idem | N/A — é a própria superfície | N/A |
| **Import Claude Code** | IMPL `adapters/claude_code.py` (`parse_session`, cap 256 MiB) | AUSENTE — sem rota de import; entra via CLI → `IngestPipeline` (mesma validação da ingest) | IMPL `import-claude` (`main.py:1350`) | N/A — SDK exporta traces nativas, não importa dumps | N/A — idem | N/A — read-only | N/A |
| **Import OTLP** | IMPL `otlp/normalize.py`, `otlp/openinference.py` | IMPL `POST /v1/otlp/traces` (push ao vivo, JSON only; protobuf → 415) | IMPL `import-otel` + `ingest -f otlp|auto` (`main.py:1393,220`) | N/A — exporter fala `TraceIngest` nativo, não OTLP | N/A — idem | N/A — read-only | N/A |

## 2. Inventário de rotas HTTP (52 operações / 47 paths + favicon)

Verificado contra `create_app().openapi()` — 47 paths no schema,
exatamente este conjunto; `GET /favicon.ico` existe fora do schema
(`include_in_schema=False`, 204).

Tier de auth: **W** = writer (`check_auth`), **R** = reader
(`read_deps` — aberto salvo `auth_reads`/`keys`), **A** = admin
(`admin_deps`).

| Método | Path | Auth | Resposta |
|---|---|---|---|
| POST | `/v1/traces` | W | `{accepted_spans, updated_spans, duplicate_spans, traces, sanitize, warnings}` |
| POST | `/v1/otlp/traces` | W | idem ingest |
| GET | `/v1/traces` | R | `{items, limit, offset}` — `started_at` DESC |
| GET | `/v1/traces/{trace_id}` | R | contrato `Trace` completo / 404 |
| GET | `/v1/sessions` | R | `{items, limit, offset}` — `last_activity_at` DESC |
| GET | `/v1/tools` | R | `{items, limit, offset}` — `calls` DESC |
| GET | `/v1/graph/traces/{trace_id}` | R | `ExecutionGraph` / 404 |
| GET | `/v1/graph/traces/{trace_id}/critical-path` | R | `{path, total_ms, cycles}` / 404 |
| GET | `/v1/graph/traces/{trace_id}/bottlenecks` | R | `{items}` (sem envelope) / 404 |
| GET | `/v1/graph/traces/{trace_id}/analysis` | R | centralidade/concentração / 404 |
| GET | `/v1/graph/sessions/{session_id}` | R | session graph + `stats.truncated` |
| GET | `/v1/traces/{trace_id}/cost` | R | cost report + `pricing_source` + `waste[]` / 404 |
| GET | `/v1/analytics/pricing` | R | `{source, models}` |
| GET | `/v1/traces/{trace_id}/failure` | R | `FailureReport` / 404 |
| GET | `/v1/traces/{trace_id}/replay` | R | timeline `{entries, total_ms, unclosed_spans}` / 404 |
| GET | `/v1/traces/{trace_id}/incident` | R | bundle + `export_sanitize` / 404 |
| GET | `/v1/traces/{trace_id}/security` | R | `{observations, counts, mode:"observer"}` / 404 |
| GET | `/v1/traces/{trace_id}/stream` | R | SSE `text/event-stream` / 404 |
| GET | `/v1/traces/{trace_id}/eval` | R | `{trace_id, results}` / 404 |
| POST | `/v1/eval/run` | R | `EvalReport` / 404 / 422 |
| POST | `/v1/eval/compare` | R | compare deltas + `gate?` / 404 / 422 |
| GET | `/v1/traces/{trace_id}/efficiency` | R | roll-up / 404 |
| GET | `/v1/traces/{trace_id}/recommendations` | R | `{recommendations, note}` / 404 |
| POST | `/v1/intel/efficiency` | R | frota + `missing_trace_ids?` |
| GET | `/v1/traces/{trace_id}/correlate` | R | sinais temporais / 404 |
| POST | `/v1/intel/correlate` | R | tabelas de co-ocorrência |
| POST | `/v1/intel/regress` | R | deltas de cohort + `missing_trace_ids?` |
| GET | `/v1/traces/{trace_id}/loops` | R | findings heurísticos / 404 |
| PUT | `/v1/datasets/{dataset_id}` | W | `{dataset_id, project_id, stored:true}` |
| GET | `/v1/datasets` | R | **array puro** por `dataset_id` |
| GET | `/v1/datasets/{dataset_id}` | R | metadados / 404 |
| PUT | `/v1/datasets/{dataset_id}/versions/{version}` | W | `{dataset_id, version, cases}` / 404 / 409 |
| GET | `/v1/datasets/{dataset_id}/versions` | R | **array puro** por `version` / 404 |
| GET | `/v1/datasets/{dataset_id}/versions/{version}` | R | versão + `cases` por `case_id` / 404 |
| PUT | `/v1/experiments/{experiment_id}` | W | `{experiment_id, project_id, stored:true}` |
| GET | `/v1/experiments` | R | `{items, limit, offset}` por `experiment_id` |
| GET | `/v1/experiments/{experiment_id}` | R | spec + `runs` + `run_limit`/`run_offset` / 404 |
| POST | `/v1/experiments/{experiment_id}/run` | W | `{run_id, run_at, report}` / 404 / 422 |
| GET | `/v1/analyze/specialists` | R | `{specialists}` (11 nomes) |
| POST | `/v1/analyze/{specialist}/{trace_id}` | R | `{run_id, stop_reason, findings, …}` / 404 / 422 |
| POST | `/v1/alerts/evaluate` | R | `{fired, evaluated, project_id}` |
| POST | `/v1/alerts/rules` | A (unscoped) | `{rules, path}` |
| DELETE | `/v1/alerts/rules/{rule_id}` | A (unscoped) | `{rules}` / 404 |
| GET | `/v1/ops/summary` | R | `{project_id, stats, recent_traces, recent_failures, note}` |
| GET | `/v1/capabilities` | R | `{hosts, note}` |
| GET | `/v1/health` | R | `{status, version, storage, stats?}` |
| GET | `/v1/metrics` | R | `{counters, gauges, histograms}` |
| POST | `/v1/admin/keys` | A | `{key_id, secret, role, projects, warning}` |
| GET | `/v1/admin/keys` | A | **array puro** por `key_id` |
| DELETE | `/v1/admin/keys/{key_id}` | A | `{key_id, revoked:true}` / 404 |
| GET | `/v1/admin/audit` | A (unscoped) | **array puro** `ts_ns` DESC |
| POST | `/v1/admin/prune` | A | `{deleted, before_ns}` |

## 3. Contrato público — notas normativas

### Paginação e bounds

- `limit` nas listas paginadas: **clampa a 1..500** (`max(1, min(limit,
  500))`), defaults: `50` (traces), `100` (sessions, tools, experiments,
  audit), `500` (detalhe de experiment — pagina `runs` via
  `run_limit`/`run_offset`). `limit=0` retorna 1 item (clamp), nunca erro.
- **Exceção**: `GET /v1/admin/audit` rejeita `limit` fora de `0..500`
  com `422` — é a única rota que não clampa.
- `offset`: clampa a ≥0; **int64 é bound de contrato** — `offset` ou
  `min_total_tokens` > `2^63−1` → `422` (nunca um bind que vira 500 no
  driver). Datas ISO e durações em ms passam pelo mesmo bound após
  conversão para ns.
- Bounds específicos: `top` (bottlenecks) 0..100 · `repeat_threshold`
  (loops) 1..50 · `limit` (session graph) 1..500, default 200, com
  `stats.truncated` quando a página enche · `trace_ids`/`cohort` 1..500
  · `evaluators` 1..100 · regras de alerta ≤100 (inline+salvas
  combinadas; arquivo salvo também tem cap de 100).
- `POST /v1/intel/correlate` `limit`: clampa a **0..500**, default 200;
  valor não-inteiro cai silenciosamente no default (única coerção
  leniente do grupo — ver Divergências pendentes).
- Corpo: `max_payload_bytes` (default 8 MiB) lido em **streaming** —
  aborta no primeiro byte excedente (`413`); `Content-Length` é só
  fast-path. Profundidade JSON: bound **lexical de 512 níveis** de
  `{`/`[` fora de strings, idêntico em todas as plataformas → `400`.
- `warnings[]` de ingest: cap de 200 + nota de overflow.

### Envelopes e ordenação

- `{items, limit, offset}`: `traces`, `sessions`, `tools`,
  `experiments`. `bottlenecks` devolve `{items}` **sem** envelope.
- **Array puro** (sem envelope): `datasets`, `datasets/*/versions`,
  `admin/keys`, `admin/audit`.
- Ordenações determinísticas (store): traces `started_at` DESC +
  (`project_id`, `trace_id`) · sessions `last_activity_at` DESC +
  `session_id` · tools `calls` DESC + `tool.name` · datasets
  `dataset_id` · versions `version` (cases por `case_id`) · experiments
  `experiment_id` (runs por `run_id`) · api_keys `key_id` · audit
  `ts_ns` DESC + `event_id` · spans dentro de uma trace `started_ns` +
  `span_id`.

### Política de erros

- Envelope: rejeições autorais → `{"detail": "<string>"}`; `422` de
  validação de query-param pelo próprio FastAPI (ex.: `limit=abc`) →
  `{"detail": [{loc, msg, type, …}]}`. `500` = handler padrão
  Starlette — texto puro `Internal Server Error`, **sem vazar**
  exceção/payload.
- `400` JSON malformado ou profundidade > 512 · `401` chave
  ausente/inválida · `403` role insuficiente ou `project_id` fora do
  escopo · `404` recurso inexistente · `409` dataset version já existe
  (append-only, `INSERT … DO NOTHING` atômico) · `413` acima de
  `max_payload_bytes` · `415` Content-Type protobuf/octet-stream em
  `/v1/otlp/traces` · `422` contrato/shape inválido (inclui string
  não-codificável — lone surrogate — e float não-finito em spec de
  experiment) · `429` rate limit **só de ingest**
  (`rate_limit_per_minute`, default 0=off) · `500` sem vazamento.
- Strings não-codificáveis (lone surrogates — bytes WTF-8 /
  surrogateescape em query, path ou body): **leitura** trata o filtro
  como ausente — página vazia, `404` no get, contagens honestamente
  zeradas em `stats` (`sqlite.py:_utf8_ok:253`); **escrita** rejeita com
  `ValidationError` → `422` (`_require_utf8:270`). Nunca um
  `UnicodeEncodeError` do driver virando `500` — invariante
  "o boundary domina o erro", não o sqlite3.
- `stop_reason: "unknown_specialist"` retorna **200** com os nomes
  disponíveis, não 404 (design).
- MCP (não-HTTP): `initialize` obrigatório antes de qualquer método
  exceto `ping` (`-32602`); argumento malformado → `-32602 "invalid
  params"`; exceção interna → `-32603` **opaco** (só o nome do tipo,
  nunca a mensagem); "trace not found" é payload `{"error": …}` com
  `isError: true` — not-found é dado, não falha.

### Idempotência e null-vs-ausente

- Ingest: re-entrega **idêntica** → `duplicate_spans`; re-entrega
  **alterada** (merge, ex.: `end_time` tardio) → `updated_spans`;
  aceite novo → `accepted_spans`.
- Dataset: metadados são **upsert** (`PUT` repete = atualiza); versions
  são **append-only** — mesmo `(dataset, version)` → `409`, nunca
  mutação in-place. Experiment: spec é upsert; cada `run` anexa a
  `experiment_results` (append-only). Regra de alerta: replace por
  `rule_id`. `admin/keys`: `secret` exibido uma única vez (só o hash é
  guardado); revoke é soft-delete (`revoked_at`).
- `null` = evidência ausente, nunca zero inventado: `estimated_usd`
  null quando modelo sem preço; `score: null` + `details.reason` em
  eval; `created_at` null = timestamp não observado (cliente-supplied;
  a API não fabrica); `stats` **omitido** (não null) em `/v1/health`
  para chave multi-projeto.
- Ausência vira evidência: `missing_trace_ids` em `intel/efficiency` e
  `intel/regress` (ids pedidos inexistentes → listados, não 404);
  `insufficient_data` com números visíveis em compare/correlate;
  janela de `ops/summary`/métricas derivadas = ≤200 traces recentes,
  declarada em `note`.

### Auth e escopo

- Modos: `none` (local single-user = admin implícito), `key` (segredo
  compartilhado = admin), `keys` (chaves gerenciadas com roles
  `reader`⊂`writer`⊂`admin` e `projects[]` de escopo — vazio =
  unscoped). `X-Forge-Lens-Key` carrega a credencial.
- `project_id` omitido resolve para `cfg.project_id` na checagem **e**
  na query — uma chave com escopo não vaza para leitura global pelo
  parâmetro omitido. Escopo vale para leitura e escrita.
- Restrições de admin com escopo: não emite chave fora do próprio
  escopo, não revoga chave fora do escopo, não vê `admin/audit`, não
  grava `alerts/rules` (globais), não faz `prune` global
  (`all_projects:true` exige unscoped).
- Rotas globais sem `_require_scope` sob `read_deps`: `/v1/metrics`,
  `/v1/analytics/pricing`, `/v1/analyze/specialists`,
  `/v1/capabilities` — dados do servidor/registry, não do tenant; uma
  chave com escopo as alcança por design (observação, não bug).
- POSTs de consulta (`/v1/eval/*`, `/v1/intel/*`, `/v1/analyze/*`,
  `/v1/alerts/evaluate`) são **read-tier**: em `none`/`key` sem
  `auth_reads` ficam abertos por design (local-first); só leem dados.

## 4. Divergências encontradas e corrigidas (este ciclo)

1. **api.md — tier writer incompleto**: faltavam `PUT
   /v1/experiments/{id}` e `POST /v1/experiments/{id}/run` (ambos usam
   `check_auth`, `app.py:1338,1454`). Corrigido.
2. **api.md — `400` ausente em `POST /v1/traces`**: JSON malformado
   (`json_invalid`) e profundidade > 512 retornam `400`, não `422`
   (`app.py:346-355`). Corrigido + bound de 512 documentado globalmente.
3. **api.md — envelope de erro não documentado**: `{"detail": str}` vs
   `{"detail": […]}` (422 de query-param do FastAPI) e `500` texto puro.
   Documentado.
4. **api.md — `GET /v1/traces` sem default/ordenação**: `limit=50` e
   ordenação `started_at` DESC + tiebreak (`sqlite.py:1073`) agora
   declarados.
5. **api.md — convenção de envelopes**: `experiments` faltava na lista
   de envelopes `{items, limit, offset}`; nota do `admin/audit` (422 em
   vez de clamp) reforçada.
6. **api.md — `GET /v1/experiments/{id}`**: campos `run_limit`/
   `run_offset` e defaults de paginação documentados.
7. **api.md — `intel/correlate`**: `limit` clampa a 0..500 (não 1..500)
   e não-inteiro cai no default — documentado como está no código.
8. **api.md — `POST /v1/experiments/{id}/run`**: `404` também cobre
   trace de cohort inexistente (resolve via `_get_trace_or_404`).
9. **api.md — `POST /v1/traces`**: semântica do envelope corrigida —
   `project_id`/`source` só preenchem quem ficou com `default`/
   `unknown`; warning de clock skew por `sent_at` documentado.
10. **api.md — `GET /v1/admin/keys`**: ordenação por `key_id`
    declarada.
11. **sdk-python.md / sdk-typescript.md — nota de cobertura**: a frase
    "não coberto de propósito" omitia que `/v1/experiments*`, os writes
    `PUT /v1/datasets/*` e `POST|DELETE /v1/alerts/rules` também não
    têm método — lacuna real agora declarada.

## 5. Divergências pendentes (registradas, NÃO corrigidas aqui)

1. **`POST /v1/intel/correlate` — `limit` leniente**
   (`app.py:1072-1075`): um `limit` não-inteiro (`"abc"`, `1.5`,
   `true`) cai silenciosamente no default 200 em vez de `422` — único
   parâmetro do grupo eval/intel que não rejeita entrada malformada.
   Candidato a decisão de política (rejeitar vs. coagir), não a bug
   corrigível pelo auditor.
2. **`docs/reference/cli.md` — assinatura do `archive` desatualizada**
   (fora do meu escopo): o doc mostra `forge-lens archive <trace_id>
   [--out file]` "exporta JSONL por trace", mas o comando real é
   `forge-lens archive --before ISO --out FILE [--delete --yes]
   [--append]` (`cli/main.py:1248`) — sem argumento posicional,
   `--before` e `--out` obrigatórios, e exporta a **janela** de traces
   encerradas antes do cutoff (não uma trace). Encaminhar ao dono de
   `cli.md`.
3. **SDKs (Py e TS) sem superfície de experiments**: as 4 rotas
   `/v1/experiments*` não têm método em `LensClient`/`LensClient` TS —
   lacuna de paridade (Studio cobre; SDK não). Registrado; adicionar
   métodos é decisão de produto, não drift de doc.
4. **CLI sem verbos para experiments, alerts, specialists, frota**
   (`intel/efficiency|correlate|regress`), `eval run`/`compare` e
   `audit`: o acesso existe via API/SDKs, mas a CLI local (que opera
   sem servidor) não expõe essas capacidades — `import-claude`/
   `import-otel`/`archive`/`install` são CLI-only na direção oposta.
   Assimetria registrada para avaliação de roadmap.
5. **Studio sem páginas para tools, security, efficiency,
   recommendations, correlate, incident, specialists, admin, alerts
   (edição de regras), metrics/health**: `api.ts` nem sequer chama
   essas rotas — cobertura deliberada do MVP ou lacuna de UI a
   decidir.
6. **`alerts_evaluate` — `use_saved` por truthiness** (`app.py:1806`):
   qualquer valor truthy (`"yes"`, `1`) ativa o merge de regras salvas;
   não há validação `isinstance(bool)`. Leniente como (2), menor.
7. **MCP `lens.list_traces` coage `limit` via `int()`**
   (`mcp_server.py:179`): `1.9` trunca silenciosamente; valores
   não-numéricos → `TypeError` → `-32602`. Consistente com o contrato
   de erro do servidor, apenas menos estrito que a API.
