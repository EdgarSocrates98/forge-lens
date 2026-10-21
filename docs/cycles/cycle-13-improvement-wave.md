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

## Docs

- `guides/operations-runbook.md` (comandos verificados char-a-char),
  `guides/troubleshooting.md` (symptom→cause→fix de modos reais de
  falha), README links; `cli.md`/`mcp.md`/`api.md`/`security/retention.md`
  sincronizados com o código (drift anterior corrigido).

## Residuals — DOCUMENTED_ONLY

- `lens.sessions` MCP: `limit` >500 clamps (API também); paginação por
  `offset` existe na store/API mas o tool MCP só expõe `limit`.
- Saved views legadas: toleradas, campos novos ausentes → defaults.
- `GET /v1/tools` (stats de ferramentas por frota) não existe —
  stats de tool saem por-trace via `efficiency`/`cost`.
