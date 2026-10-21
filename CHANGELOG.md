# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/), SemVer.
`Unreleased` acumula mudanças até o próximo tag.

## [Unreleased]

### Added — improvement wave (post-review)

- `GET /v1/traces` server-side filters: `status`, `session_id`, `tool`,
  `q`, `since`, `until`, `min_duration_ms`, `max_duration_ms`
  (traces sem `ended_at` são excluídas por filtros de duração); os
  mesmos filtros em `forge-lens traces list`.
- `GET /v1/sessions`: roll-up por `session_id` (traces, spans,
  first_started_at, last_activity_at) + `lens.sessions` no MCP.
- `GET /v1/tools`: roll-up de frota por `tool.name` + `forge-lens tools`
  + `lens.tools` no MCP (paridade CLI/API/MCP).
- MCP stdio: `lens.detect_loops`, `lens.trace_efficiency`, `lens.cost`,
  `lens.failure`, `lens.list_datasets`, `lens.sessions`, `lens.tools`,
  `lens.capabilities` (11 tools read-only).
- Avaliadores `contains`, `regex`, `tool_call_count`;
  `latency_budget` aceita `max_duration_ms` (`budget_ms` segue como alias).
- Studio: barra de filtros completa no Trace Explorer (deep-linkável,
  saved views) e nova view `/sessions`.
- Docs: `guides/operations-runbook.md`, `guides/troubleshooting.md`;
  referências `cli.md`/`mcp.md` e `security/retention.md` sincronizadas
  com o código.

### Added — cycles 06–12

- Datasets versionados + casos (ground truth), avaliadores
  `exact_match`/`token_f1`/`allowed_tools`/`task_completion`, gate de
  regressão em `/v1/eval/compare`.
- Auth modes `none|key|keys`: API keys com hash, roles
  reader/writer/admin, escopo por projeto, isolamento de tenants,
  endpoints `/v1/admin/{keys,audit,prune}`, audit trail sem segredos,
  `forge-lens prune|archive`, TLS em `serve`.
- Loop detection (`/v1/traces/{id}/loops`), runtime de especialistas
  com budget/cancelamento (`/v1/analyze/*`), alertas por threshold
  (`/v1/alerts/evaluate`), dashboard ops (`/v1/ops/summary`).
- SSE replay `/v1/traces/{id}/stream`, benchmark real medido
  (scripts/benchmark.py → docs/benchmarks).
- Inteligência avançada: `/v1/intel/{efficiency,regress}`,
  `/v1/traces/{id}/{efficiency,recommendations}`,
  `/v1/graph/traces/{id}/analysis`.
- Adaptadores: matriz honesta de capacidades (`/v1/capabilities`),
  importer Claude Code JSONL (`import-claude`), servidor MCP stdio
  (`mcp-serve`), lifecycle de instalação com receipts
  (`install-status`), 7 skills em `skills/`.
- Release engineering: `scripts/gen_sbom.py` (CycloneDX do ambiente
  instalado), workflow release.yml com OIDC + gate de versão.

## [0.1.0] — baseline

- Cycles 00–05: contratos, ingest OTLP/nativo, store SQLite WAL,
  sanitização, SDKs Py/TS, CLI, grafo, custo/tokens, replay/incidente,
  eval determinístico. 270+ testes, ruff/mypy-strict limpos.
