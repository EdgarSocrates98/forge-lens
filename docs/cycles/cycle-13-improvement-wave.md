# Cycle 13 — improvement wave (post-review)

Status: IMPLEMENTED. Executed by 5 parallel subagents on disjoint file
scopes + parent integration. Every claim below is backed by tests in
`tests/unit/test_filters.py` (20), `test_mcp_tools.py` (13),
`test_evaluators_new.py` (15) and `apps/studio` specs (11).

## Backend — query surface

- `TraceStore.list_traces` gained `status`, `session_id`, `since_ns`,
  `until_ns`, `min_duration_ns`, `max_duration_ns`, `q`, `tool` — all
  AND'd, `project_id=None` stays the all-projects wildcard, LIKE values
  escape `\`/`%`/`_` (so `tool="%"` matches only the literal name).
- Duration semantics: `ended_ns IS NULL` rows are **excluded** whenever
  a duration filter is set — documented, not silent.
- `GET /v1/traces` exposes all filters with 422 validation (bad ISO,
  non-finite/negative durations, `status` fora de {ok,error,unset});
  `forge-lens traces list` espelha via flags.
- `GET /v1/sessions` + `SQLiteTraceStore.list_sessions`: roll-up
  `GROUP BY session_id` (NULL excluded), `last_activity_at` =
  max(ended/started) por sessão — nunca NULL enquanto houver ≥1 trace.
  Ordenação determinística `last_activity DESC, session_id ASC`.
- `GET /v1/tools` + `SQLiteTraceStore.list_tools`: roll-up de frota
  sobre spans `kind=tool` via `json_extract(tool.name)` — calls,
  traces, errors, avg_duration_ms; spans sem `tool.name` caem em
  `(unnamed)` em vez de sumir da contagem. Coberto na matriz de
  tenancy 403.

## MCP observer — 3 → 10 tools

`lens.detect_loops`, `lens.trace_efficiency`, `lens.cost` (mesmo
`PricingRegistry` da API — `pricing_file` inválido → exit 2),
`lens.failure`, `lens.list_datasets`, `lens.sessions`,
`lens.capabilities`. Todas read-only sobre store readonly; arg
malformado → JSON-RPC error sem matar o loop.

## Eval engine — 4 → 8 avaliadores determinísticos

`contains`, `regex` (pattern inválido → `evaluator_error`, nunca
exceção), `latency_budget` com `max_duration_ms` (`budget_ms` alias),
`tool_call_count` (bounds `min`/`max`, filtro `tool`). Todos `(fn,
binary=True)` — elegíveis ao z-test de `eval/compare`. Trace sem
`ended_at` → `score=None` com razão honesta.

## Studio

- Filter bar: `q`, `tool`, `session_id`, min/max duration, since/until
  (datetime-local → UTC ISO) — todo o estado sincroniza com a URL
  (deep-linkável) e round-trips em saved views.
- Nova rota `/sessions`: tabela com prefix-search, paginação por offset
  (50/página, dedupe em live-ingest), link → `/traces?session_id=…`.
- Trace detail: painel de eval — GET `/traces/{id}/eval` no load +
  runner via checkboxes sobre os 12 avaliadores do registry →
  `POST /v1/eval/run` (`{trace_ids:[id], evaluators:[{name}]}`);
  `score:null` renderiza `n/a` + reason — nunca veredito inventado.

## Docs

- `guides/operations-runbook.md` (comandos verificados char-a-char),
  `guides/troubleshooting.md` (symptom→cause→fix de modos reais de
  falha), README links; `cli.md`/`mcp.md`/`api.md`/`security/retention.md`
  sincronizados com o código (drift anterior corrigido).

## Follow-on increments (landed após o report inicial)

- `forge-lens sessions` — CLI parity para `/v1/sessions` (flags `--db`,
  `--project`, `--limit -n` ≤500, `--json`).
- `lens.{sessions,tools}` MCP: `offset` exposto (shared `_page_args`
  valida limit/offset; malformado → JSON-RPC -32603).
- Studio trace-detail: UI de params por avaliador — fields tipados por
  kind (number/int/bool/list/json/text) serializam para
  `{name, params}`; valor malformado bloqueia antes do POST; campo
  vazio = omitido → `evaluator_error`/`n/a` honesto (comportamento
  anterior preservado).
- Studio `/datasets`: list/search, create dataset, create version com
  cases (input/expected JSON), e painel "compare cohorts" que posta o
  body real de `/v1/eval/compare` e renderiza deltas + gate verdict.
- Benchmark smoke layer (`tests/bench`, marker `benchmark`): entrega a
  promessa do ciclo-09 de "real benchmark runs" como camada contínua —
  corpus determinístico de 1k traces semeadas pelo `IngestPipeline`
  real (`capture=full`), timings de parede sobre ingest (500 traces,
  `capture=metadata`), `list_traces` com cada filtro, roll-ups
  `list_sessions`/`list_tools`, `get_trace`, `build_trace_graph`
  (2k spans) / `build_session_graph` (40 traces) e bateria eval
  (fetch 200 + registry completo de 12 avaliadores). Assertivas de
  correção por seletividade exata; único teto temporal é generoso
  (ingest < 30 s, ~10× o medido — tripwire, não SLA). Sumário via
  `pytest_terminal_summary` + `--bench-report=PATH` (JSON com
  ambiente). Doc em `docs/reference/benchmarks.md` com números medidos
  nesta máquina e caveats honestos.
- SDK Python read-side (`src/forge_lens/sdk/query.py`): `LensClient` +
  `QueryError` cobrem todas as rotas GET de leitura (`traces` com todos
  os filtros, `sessions`, `tools`, `graph/*`, `cost`/`failure`/`replay`/
  `incident`/`security`/`eval`/`efficiency`/`recommendations`/`loops`,
  datasets + versions, meta) **e** os POSTs de consulta sob `read_deps`
  (`eval/run`, `eval/compare`, `intel/efficiency`, `intel/regress`,
  `analyze/*`, `alerts/evaluate`). urllib-only como `HTTPExporter`;
  `from_config()` usa a mesma cadeia env/TOML; `X-Forge-Lens-Key` quando
  `api_key` definida (role `reader` basta). `GET /v1/traces/{id}/stream`
  (SSE) fica fora de propósito — não é recurso JSON. Testes em
  `tests/unit/test_query_client.py` (22): uvicorn real em loopback
  (mesmo padrão do e2e) + `urlopen` fakeado para shapes/erros.
- CLI read-parity wave (`src/forge_lens/cli/main.py` + 26 testes em
  `tests/unit/test_cli_read_parity.py`): `capabilities` (mesmo payload
  de `/v1/capabilities`/`lens.capabilities`, sem store), `stats`
  (mesma composição de `/v1/ops/summary` — stats + janela de 200 +
  recent_failures), `datasets list|show` (metadata + versions via
  store), e as análises por trace `failure`/`loops`
  (`--repeat-threshold` clamp 1..50 como a rota)/`efficiency`/
  `recommendations`/`security`/`replay` chamando as mesmas funções do
  core que as rotas usam. Trace/dataset inexistente → mensagem limpa +
  exit 2 (desvio documentado: os comandos antigos `cost`/`incident`/
  `traces show` usam exit 1 — os novos seguem a convenção do spec).
  `import-otel <file>` importa `ExportTraceServiceRequest` JSON com o
  fluxo da rota OTLP (bound `max_payload_bytes`, quarentena → warnings,
  `provenance=imported`, `IngestPipeline`); admin/audit/alerts/eval-POST
  ficam fora de propósito (superfície admin / já coberto por `eval`).
- `docs/reference/api.md` — sync completa pós-auditoria: a claim
  "`api.md` sincronizados" estava furada (o arquivo parava em
  `GET /v1/metrics`, ~25 rotas sem doc). Reescrita cobrindo as 46
  rotas `/v1` — datasets CRUD (6), `traces/{id}/security|stream|loops|
  efficiency|recommendations`, `graph/.../analysis`, `intel/efficiency|
  regress`, `analyze/specialists` + `analyze/{s}/{id}`, `alerts/*` (3),
  `ops/summary`, `capabilities`, `admin/keys|audit|prune` (5) — com
  auth por rota (reader/writer/admin), bounds de query params e shape
  de resposta verificados contra `api/app.py`. README: corrigida a
  redação que vendia demais o Studio ("evidence tabs"/"ops dashboard"
  → painel evidence com blocos JSON + tabelas de eval/replay; ops =
  resumo em JSON). Drift flagrado na auditoria, corrigido aqui.
- Entidade `Experiment` persistida (audit gap 06.3): contrato
  `contracts/v1/experiment.py` (+ `schemas/v1/experiment.json`),
  tabelas `experiments` + `experiment_results` (append-only) e rotas
  `PUT/GET /v1/experiments{,/{id}}` + `POST /v1/experiments/{id}/run`
  que executa o MESMO caminho de `/v1/eval/compare` (evaluate_traces →
  compare_runs → gate opcional) e grava o report — runs imutáveis,
  status `open→completed`, escopo por projeto como datasets.
  `docs/reference/experiments.md` + 7 testes em
  `tests/unit/test_experiments.py`.
- `eval/compare` statistics (gap 06.6): intervalos de Wilson a 95%
  (`ci_a`/`ci_b`) nas pass rates binárias — descritivos mesmo abaixo do
  piso — e Mann-Whitney U (aprox. normal com correções de continuidade
  e de empates, n≥10/lado, amostras brutas por resultado) para métricas
  contínuas, que deixam de ser `not_tested`; `test` nomeia o teste por
  métrica, `not_tested` agora cobre só avaliadores fora do registry, e
  `multiple_comparisons` avisa quando >1 teste roda sem correção.
  Gate inalterado — regressões contínuas significativas (MWU +
  `delta` < 0) agora alcançam `fail_on_significant_regression`.
  16 testes em `tests/unit/test_eval_compare.py`.
- Eval engine 12 → 17 avaliadores (gaps §06.2/§06.5):
  `schema_validation` (subconjunto JSON-Schema honesto — apenas
  `type`/`properties`/`required`/`items`; keyword fora do subconjunto →
  `evaluator_error`; string JSON parseia para objeto/array, escalar
  permanece string), `evidence_coverage` (fração de `required_attrs`
  observada trace-level, match exato ou prefixo `attr.`; score =
  veredito vs `min_coverage`, fração crua em `details.coverage`),
  `human_review` (marcador de fila — sempre `score=None` +
  `pending_human_review` + rubric ecoado, nunca score fabricado),
  `precision_recall` (P/R/F1 de conjunto — modo `tool` sobre tool
  names invocadas ou modo `field` extraindo itens; contínuo → MWU) e
  `claims_grounded` (overlap lexical saída↔`required_source_attr` ≥
  `min_overlap`; **heurística documentada, não checagem factual** — sem
  fonte observada → `None`, pois em `capture=metadata` corpos são
  dropped e ausência ≠ evidência de claim não suportado). LLM-as-judge
  fica fora do core determinístico por regra. 32 testes em
  `tests/unit/test_evaluators_wave2.py`; bateria bench cobre as 17 keys.
- Golden-scenario corpus (gap 05.1): `tests/golden/` — 47 cenários
  declarativos (`input`/`expected`/`forbidden`/`evidence`/`provenance`)
  executados por um único runner genérico contra store em memória,
  `IngestPipeline`, `TestClient` e o servidor MCP real; o corpus se
  auto-valida (schema estrito, `MIN_SCENARIOS`) e documenta
  comportamento real verificado — incluindo `partial_trace` persistindo
  após merge tardio e colisão in-batch de `span_id` virando merge +
  `conflict`. Formato em `docs/reference/golden-scenarios.md`.
- Métricas derivadas de alerta (gap §07.5): `ops/alerts.py` ganha
  `resolve_metrics` — cada nome em `METRICS` (4 → 14) tem resolver
  próprio sobre `_Window` materializada uma vez por avaliação.
  Contadores seguem sobre `store.stats` (corpus do projeto);
  duração/custo/tokens/loops/erro-por-trace resolvem sobre a janela
  limitada das ≤200 traces mais recentes por `started_at` (mesma
  convenção de `ops/summary`, nunca scan de frota). Novos:
  `avg_duration_ms`, `max_duration_ms`, `p95_duration_ms`
  (nearest-rank; `ended_at` ausente exclui a trace), `error_traces` +
  `error_trace_rate` (nível de trace), `token_total` (fallback
  idêntico a `trace_cost`), `cost_total_usd` + `cost_per_trace_usd` +
  `unpriced_model_count` (uso sem preço no registry é excluído e
  contado separadamente — gasto cego alertável), `loop_traces`
  (`detect_loops` bound na janela). `eval_pass_rate` não é oferecido:
  `/v1/eval/run` é efêmero e `experiment_results` são deltas por
  experiment/cohort — sem escalar honesto. `/v1/alerts/evaluate`
  alimenta `only=` com as métricas das regras (regra só de contadores
  não escaneia traces). Tabela métrica/unidade/bound em
  `docs/reference/api.md`; 12 testes em `test_alert_metrics.py`.

## Failure investigation (gaps §05.3/§05.4/§12.7 fechados nesta wave)

- §05.3 — taxonomia completa: 7 categorias novas em
  `investigate/classify.py` detectadas **só por evidência registrada**
  (`invalid_arguments`, `schema_mismatch`, `auth_error`,
  `missing_permissions`, `retry_exhaustion`, `agent_cancelled`,
  `budget_exceeded`). Sinais: HTTP status em attrs
  (`http.status_code`/`http.response.status_code`), `exception.type`
  em attrs **e eventos OTel**, counters `retry.*`/`attempt*` (max
  registrado ou `RETRY_EXHAUSTION_THRESHOLD=3`), flags booleanas,
  texto de status/eventos. Ordem de especificidade revisada:
  `budget_exceeded` antes de `rate_limit` (429+quota é exaustão de
  conta), `schema_mismatch` antes de `invalid_arguments`,
  `agent_cancelled` depois das causas (cancel é como outra causa se
  manifesta — concorrentes visíveis nas hipóteses). Cancelamento com
  sinal forte em span não-errored marca `failed=true`; texto fraco
  ("order cancelled") em span saudável não conta — teste dedicado.
  `evidence` ganha `signals`/`names` mantendo `status`/`message`
  legados (goldens intactos).
- §05.4 — `Hypothesis`/`HypothesisStatus` + `build_hypotheses` em
  `investigate/hypothesis.py`: `{hypothesis_id=trace_id:categoria,
  label, symptom, supporting_evidence, contradictory_evidence,
  missing_evidence, suggested_verification, confidence
  (observed|inferred), status}`. Status honesto: `supported` ≥2
  sinais, `proposed` 1 sinal, `refuted` somente quando suporte é
  fraco **e** rival forte existe no mesmo span, `unverifiable` para
  falha sem assinatura. Bounded (`MAX_HYPOTHESES=10`), ordenado
  deterministicamente (primária primeiro).
- §12.7 — `suggest_evaluators(reports)` em
  `investigate/eval_suggest.py`: mapeia categorias (single ou
  `recurring` ≥2 traces) para specs candidatas de avaliador +
  dataset-case (`{kind,name,params,params_required,rationale,
  evidence,classification:"candidate"}`) — `invalid_arguments` →
  `allowed_tools`/`tool_call_count`, `timeout` → `latency_budget`,
  `budget_exceeded` → `token_budget`, etc. `unknown`/saudável não
  gera nada; **datasets nunca são modificados**.
- `failure_investigator` embute `hypotheses` (cap
  `context.max_hypotheses`, default 5, `hypothesis_count` +
  `hypotheses_truncated` declaram o corte) + `eval_suggestions`;
  bundle `/incident` ganha `hypotheses` + `eval_suggestions`.
- Docs: `docs/guides/failure-investigation.md` (taxonomia + tabela de
  sinais + modelo de hipótese), `evaluations.md` §12.7,
  `api.md` categorias/specialist/incident. 23 testes em
  `tests/unit/test_failure_categories.py`.

## Docs & community (gaps §12.9/§12.10 fechados nesta wave)

- `.github/ISSUE_TEMPLATE/` — `bug_report.md`, `feature_request.md` e
  `config.yml` (vulnerabilidade → Security Advisories; docs/roadmap
  como contact links; blank issues desabilitados).
- `docs/roadmap.md` — roadmap público com a tabela **Declared
  deferrals**: todo deferral conhecido virou descope explícito com
  racional + gatilho de revisita (encryption-at-rest §08.7, scalable
  mode §09.3, browser matrix §11.4, 1M spans §09.9, SSO/OIDC,
  re-execution replay, canais de alerta, MCP ao vivo, adapters
  codex/devin/copilot, §12.5–12.8 residuais, OTLP/protobuf, filtros
  custo/modelo + virtual scroll do Studio, eval CI gate). Regra:
  undeclared gap vira declared aqui.
- `docs/cycles/cycle-07..12-final-report.md` — split do consolidado
  `cycle-06-12.md` nos relatórios individuais que a spec §9 exige
  (escopo verificável, semântica, exclusões, evidência por ciclo);
  consolidado permanece como índice com nota de apontamento.
- `docs/adoption.md` — matriz de valor real (§12.10): quem se
  beneficia, custo de adoção, "ainda não faz" linkado ao roadmap e o
  caminho feedback→prioridade.
- `examples/sample-otlp.json` — sample trace OTLP pronto para
  `forge-lens import-otel` (§12.9: sample traces).
- `forge_lens.intel.correlation` (§05.5): `correlate_trace` pareia
  sinais intra-trace (retries→erro, cluster de falha de tool, troca de
  modelo, crescimento de contexto→erro/eval, erros→eval) com
  `evidence_span_ids` + `strength` + `caveat`, sempre
  `temporal_association`; `correlate_fleet` faz co-ocorrência
  descritiva sobre janela ≤500 (tool/modelo/sessão↔falha,
  retries↔score observado) com pisos de `insufficient_data` — zero
  claim causal, zero significância. `docs/reference/correlation.md` +
  16 testes em `tests/unit/test_correlation.py`.

## Studio — playback, experiments e evaluators (gaps §05.2/§06.7)

- Replay ganha **playback controls** de verdade: play/pause,
  step ‹/›, jump |«/»|, velocidade 0.5×–4×, posição `i/N`,
  jump-to-next error/tool/event (evidência real: `status==='error'`,
  span `kind==='tool'` via `span_id`, `kind==='event'` — nunca nome-
  guessed), teclado scopiado (←/→/space), auto-pausa na navegação
  manual e no fim; linha corrente destacada + scrollIntoView; o cap de
  200 linhas expande quando o step passa dele. Perf: `pct`/`widthPct`
  que chamavam `Date.parse` por row por CD agora são pré-computados
  em `buildRows`.
- `/experiments` (§06.7): rota dedicada — list, create (cohorts a/b
  com label + trace-ids + `config` JSON, pins de dataset com
  datalists, evaluator multiselect), detail com spec + runs history +
  **Run now** (`POST /v1/experiments/{id}/run`, gate opcional) →
  report inline.
- `compare-report.ts` compartilhado (datasets + experiments):
  deltas/gate verbatim + `multiple_comparisons`, **failure examples**
  recomputados deterministicamente via `/v1/eval/run` sobre a união
  dedupada das coortes (chunked no cap de 500 ids), e **Pareto**
  renderizado só quando métricas de qualidade × custo/tokens trazem
  means reais nos dois lados.
- Studio eval mirrors 12 → **17 avaliadores** (`evaluatorNames` em
  datasets/experiments/trace-detail + `evaluatorParams` para
  `schema_validation`, `evidence_coverage`, `human_review`,
  `precision_recall`, `claims_grounded`).

## Hardening pós-review (integração)

- §02.6 — filtros `model`/`agent`/`host`/`min_total_tokens` em
  `list_traces` (store → API → CLI `--model/--agent/--host/
  --min-tokens` → SDK Python `model/agent/host/min_total_tokens` →
  SDK TS `model/agent/host/minTotalTokens`). `model` casa
  `gen_ai.request|response.model`; `agent` casa `gen_ai.agent.name`
  ou identidade do produtor (`source.system`/`service.name`); `host`
  casa `source.host`/`host.name`; `min_total_tokens` soma
  `gen_ai.usage.total_tokens`. `source_json` serializa compacto
  (`model_dump_json`) — padrões `":"` vs `": "` divergem por coluna,
  documentado no código. **Sem filtro `cost`** — custo é derivado do
  pricing na leitura; predicado SQL de custo exigiria materializar um
  derivado (documentado em api.md/cli.md/sdk docs).
- MCP: `tools/call` exige `initialize` (pré-init → `-32602`); falha
  de dados pós-init → `result.isError` com envelope de erro — o loop
  sobrevive; exceção em tool → `-32603` sem vazar `str(exc)` crua no
  canal. Tool `lens.correlate` espelha a rota.
- `/v1/traces/{id}/incident`: bundle passa por `redact_export` no
  boundary de export (findings derivados podem ecoar corpos verbatim
  sob `capture=full`) e responde `export_sanitize:
  {redacted_keys, redacted_values, emails_masked}` — teste de
  regressão em `test_security.py` prova segredo `sk-ant-*` + email
  não escapam.
- `graph_intel` docstring corrigida (sem overclaim de métrica de
  handoff); drift de docs: `security.md` TLS/RBAC/audit refeitos
  contra o ciclo 08 real, `threat-model.md` — leitura aberta só em
  `auth_mode=none`.
- Rotas `/v1/traces/{id}/correlate` + `/v1/intel/correlate` +
  `forge-lens correlate` + `lens.correlate` + SDKs (paridade
  completa); `api.md`/`cli.md`/`mcp.md`/`sdk-*.md` sincronizados.

## Residuals — DOCUMENTED_ONLY

- `limit` >500 clamps nos roll-ups (store/API/CLI/MCP — consistente).
- Saved views legadas: toleradas, campos novos ausentes → defaults.
- `POST /v1/eval/compare`: trace-id ausente é 404 per-side (não há
  `missing_trace_ids` na resposta — UI reflete o erro verbatim).
- Side `dataset` em compare é label, não resolver — a UI documenta.
- `GET /v1/traces/{id}/failure` retorna só o `FailureReport` (contrato
  estável) — hipóteses §05.4 e sugestões §12.7 chegam via bundle
  `/incident`, specialist `failure_investigator` e
  `build_hypotheses`/`suggest_evaluators` direto; não há rota dedicada
  nem campo extra no payload de `/recommendations` (o payload de
  `intel.recommendations.recommend()` segue per-trace, sem a visão
  cross-trace `recurring` — essa agregação é library-level hoje).
- Detecção §05.3 lê apenas evidência registrada: producers que não
  gravam `exception.type`, http status ou retry counters caem em
  `unknown`/`kind buckets` — é o fallback honesto, não um bug; weak
  keyword-only matching pode sub/notificar (hipóteses declaram
  `confidence: inferred` e `missing_evidence` nesses casos).
