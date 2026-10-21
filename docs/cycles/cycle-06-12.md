# Cycles 06–12 — implementation report

Status: IMPLEMENTED (this delivery). Reports for cycles 00–05 live in
`cycle-0X-*.md`. Honest residuals are listed at the end.

> **Note (post-review wave):** the mandated per-cycle reports now exist
> as individual files — `cycle-07-final-report.md`,
> `cycle-08-final-report.md`, `cycle-09-final-report.md`,
> `cycle-10-final-report.md`, `cycle-11-final-report.md` and
> `cycle-12-final-report.md` — expanded from this consolidated summary
> with per-cycle scope, residuals and verification evidence. This file
> remains as the consolidated index/timeline. All deferrals listed in
> "Residuals" below are now formally descoped in `docs/roadmap.md`.

## 06 — versioned eval & datasets
- `contracts/v1/dataset.py` + `datasets`/`dataset_versions`/`dataset_cases`
  tables; CRUD under `/v1/datasets*` with project isolation.
- Evaluators: `exact_match`, `token_f1`, `allowed_tools` (matches
  `tool.name` attrs), `task_completion`; `apply_gate` attaches a
  regression verdict to `/v1/eval/compare` responses.

## 07 — agentic observability
- `ops/loops.py`: repeated-identical-call, retry-loop, cyclic-agent and
  no-progress findings — all `heuristic`, span_ids as evidence.
- `ops/agents.py`: 11 named specialists over a bounded runtime
  (iterations/token/timeout budgets, cooperative cancel).
- `ops/alerts.py` + `/v1/alerts/evaluate`: threshold rules over live
  aggregates; `/v1/ops/summary`: dashboard feed (bounded window, stated).
- `security/mcp_inspect.py`: observer-mode MCP/tool inspection —
  evidence only, never blocks.

## 08 — security & tenancy
- `api_keys` (hashed secrets, role, project scope) + `audit_events`.
- Auth modes `none|key|keys`; `reader|writer|admin` enforced per-route;
  `_get_trace_or_404` enforces scope centrally so a route that forgets
  `_require_scope` still can't leak; E2E covers every read route
  (traces family, graph family, ops, alerts, intel, analyze, eval)
  with a p1-scoped key against p2 data → 403 on all, plus global-stats
  scoping on health/ops/alerts.
- Retention: `prune_traces`, `forge-lens prune`, `forge-lens archive`
  (export-before-delete), `POST /v1/admin/prune`; TLS flags on `serve`.
- `api/clock.py` is the single wall-clock seam (audit/retention only).

## 09 — scalability
- Indexes on traces/spans; SSE replay `/v1/traces/{id}/stream`.
- `scripts/benchmark.py` — REAL measured numbers in
  `docs/benchmarks/README.md` (1k/10k/100k; 1M explicitly not claimed).

## 10 — ecosystem
- `adapters/capabilities.py`: honest matrix (unsupported stays
  unsupported); `adapters/claude_code.py`: JSONL importer;
  `mcp_server.py`: real stdio JSON-RPC tools/list|call;
  `install/lifecycle.py`: receipts/doctor/uninstall; 7 skills in
  `skills/` invoking real verbs.

## 11 — release engineering
- `scripts/gen_sbom.py` (CycloneDX from installed env);
  `.github/workflows/release.yml` (OIDC, version gate, gated `pypi`
  environment); `CHANGELOG.md`; `docs/versioning.md`; studio CI job.

## 12 — advanced intelligence
- `intel/efficiency.py` (per-trace + fleet: tokens/tools per success,
  retry overhead — `None` when a signal is absent);
  `intel/regressions.py` (cohort deltas, labelled `observed_difference`,
  never significance-claimed); `intel/recommendations.py` (candidates
  only); `intel/graph_intel.py` (centrality, concentration, failure
  propagation, repeated subgraphs).

## 02 — Studio (Angular 22, `apps/studio`)
- Trace Explorer (filters, sortable columns, URL-state, deep links);
  trace detail (waterfall with critical-path-style bars, span attrs,
  evidence tabs: cost/failure/loops/graph); ops view; capabilities view.
- Dark/light themes, lazy routes, typed `Api` client, zoneless signals.
- `npm run build` clean; `ng test` 3/3.

## Residuals (declared, not claimed)
- Encryption-at-rest: not implemented — `docs/architecture/security.md`
  scopes it as deployer responsibility (OS-level/encrypted FS); no
  in-process crypto claim is made.
- 1M-span benchmark not run (measured data stops at 100k).
- Multi-user SSO/OIDC: out of scope — `keys` mode covers local/team.
- PostgreSQL/ClickHouse: evaluated and deferred — no measured need at
  local-first scale; ADR required before adding (per project rules).
- Studio e2e browser suite (Playwright multi-browser): CI runs unit
  tests + build; cross-browser e2e is a documented follow-up.
- Studio list filters cover status+limit only; server-side
  duration/cost/token/model/host/agent/time-range filtering requires
  store-level query params (follow-up, not faked client-side).
- Studio virtualization is bounded rendering (200-row window +
- Deliberate decisions (auth surface): `/docs` + `/openapi.json` stay
  unauthenticated in `keys` mode — schema-only disclosure, required
  for tooling; `/v1/health` requires a reader key in `keys` mode —
  load-balancer probes must carry one (documented trade-off, not an
  oversight).
  load-more), not a CDK virtual-scroll viewport.
