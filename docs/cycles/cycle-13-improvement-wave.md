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

## Residuals — DOCUMENTED_ONLY

- `limit` >500 clamps nos roll-ups (store/API/CLI/MCP — consistente).
- Saved views legadas: toleradas, campos novos ausentes → defaults.
- `POST /v1/eval/compare`: trace-id ausente é 404 per-side (não há
  `missing_trace_ids` na resposta — UI reflete o erro verbatim).
- Side `dataset` em compare é label, não resolver — a UI documenta.
