# Cycle 14 — improvement wave: surface parity, CI gates, FORGE process parity

Status: IMPLEMENTED. Executed by 5 parallel subagents on disjoint
file scopes + parent integration and CI-fix round. Triggered by three
read-only audits: a sibling-FORGE convention comparison, a
surface-parity audit across API/CLI/SDKs/MCP/Studio, and a
quality/perf audit of `src/` + `apps/studio/`.

Every claim below is backed by tests — `tests/unit/test_cli_surfaces.py`
(34), `tests/unit/test_batch_reads.py` (~30), `tests/unit/test_boundary.py`
(4), `apps/studio` specs (97 total, +18), Playwright e2e (126).

## Wave A — CLI surface parity (`cli/main.py`, +~1000 lines)

Commands that existed only in the API/engine became CLI verbs —
all local (store-direct, never HTTP), mirroring handler semantics:

- `forge-lens analyze --list` / `analyze <specialist> <trace_id>` —
  the 11 deterministic specialists (`ops/agents.py`) were
  unreachable outside the API.
- `forge-lens audit [--action --actor --limit --offset]` — admin
  audit trail readable outside the endpoint.
- `forge-lens experiments create|list|show|run` — full persisted-
  experiment lifecycle (cohorts a/b, evaluator specs, optional
  gate, run history) mirroring `/v1/experiments` validation.
- `forge-lens alerts list|save|delete|evaluate` — alert rules in
  `<data-dir>/alert-rules.json` + on-demand evaluation, parity with
  `/v1/alerts/*`.
- `forge-lens eval-suggest <trace_id>...` — multi-report surface
  over `investigate.eval_suggest.suggest_evaluators`; with ≥2
  traces `evidence.recurring` becomes reachable (previously a dead
  code path — every call site passed singletons).
- `forge-lens intel efficiency|correlate|regress` — fleet
  intelligence (`fleet_efficiency`, `correlate_fleet`,
  `compare_cohorts`).
- `forge-lens eval --evaluator NAME --params-json` — any evaluator
  in the registry (was 3 fixed).
- `forge-lens traces list --offset N` — pagination the API already
  accepted.
- `install`/`uninstall` — receipt write failure → `ok:false` →
  exit 1, no traceback (was a raw `OSError`).

Documented asymmetries (intentional): `audit` has no `--project`
(audit events carry no project attribution); `alerts` verbs take no
`--db`/`--project` (they operate on the rules file); `intel`/
`experiments list` clamp `--limit` 0–500 (API parity) while
`audit`/`traces list` reject out-of-range.

## Wave B — Studio surface parity (`apps/studio`)

- `api.ts`: `getIncident`, `getSecurityReport`, `getEfficiency`,
  `getRecommendations`, `getCorrelate`, `listSpecialists`,
  `runAnalysis`, `getDatasetVersion`; `TraceFilter` gained `model`,
  `agent`, `host`, `min_total_tokens`, `project_id`, `offset`;
  `listTraces` returns the `{items, limit, offset}` envelope.
- trace-detail: **Incident** panel renders `eval_suggestions`,
  `hypotheses` and `waste` — fields previously visible only in CLI
  text output; collapsible security/efficiency/recommendations/
  correlate panels; specialist analyze runner via
  `POST /v1/analyze/{specialist}/{trace_id}`.
- datasets: version → cases viewer (`GET /v1/datasets/{id}/
  versions/{v}`).
- traces list: all server-side filters the API accepts + offset
  pagination (load-more), URL query-param round-trip, saved views.

## Wave C — CI gates that only existed locally

- `studio-e2e` (ubuntu, 30min): seed → `forge-lens serve` →
  production build → Playwright **chromium + firefox + webkit**
  (the suite never ran in CI before; report artifact on failure).
- `eval-gate`: `scripts/eval_gate.py` is now a real check —
  committed fixtures in `tests/fixtures/eval-gate/`; pass case →
  exit 0, regressed case → asserted exit 1 (proves the gate is not
  vacuous).
- `wheel-smoke`: `uv build` → wheel installed into a clean venv →
  `--version`/`init`/`doctor` + `serve` without the server extra
  degrades cleanly (exit 2, no traceback).
- `minimal`: `uv sync` without extras → CLI smoke + pytest subset
  without fastapi/uvicorn — proves the documented extras-
  degradation contract on every PR.

## Wave D — FORGE process parity

- `forge.json` `"lab"` block + `scripts/lab/forge_lens_lab.py` —
  `forge/BenchmarkResult/v1` emitter (the only forge in the
  workspace missing it). Runs the `tests/golden` corpus via
  pytest→junit→receipt; per-row verdict/duration/evidence_ref;
  `precision`/`recall` reported `not_available` honestly (functional
  assertions, not ranked detection).
- `tests/unit/test_boundary.py` — AST scan mechanically enforcing
  the AGENTS.md hard rules: zero network imports
  (socket/urllib/http/requests/httpx/aiohttp/subprocess/asyncio)
  and zero wall-clock reads (`time.time`, `datetime.now`…) inside
  `core/ contracts/ otlp/ sanitize/ storage/ ingest/` — exceptions
  pinned to `core/metrics.py` + `ingest/pipeline.py` (duration-only
  clocks). Alias-resolved (`import time as t`, `from time import
  time` — mutation-verified).
- `tests/conftest.py` — loopback-scoped socket guard in
  `tests/unit` + `tests/contract` (see CI-fix note below); opt-out
  via `pytest.mark.network`.
- `factory/` skeleton — `specs/{inbox,active,archive}`, `runs/`,
  `quality-gate.md`, README documenting the Loop Factory
  inbox→active→archive convention.
- Housekeeping: PR template, CODE_OF_CONDUCT, SUPPORT,
  `.gitattributes` (`eol=lf` on `tests/golden/**`, `homolog/**`,
  captured fixtures — bytes cannot drift on Windows checkouts),
  `.editorconfig`; `CONTRIBUTING.md` dead reference repointed to
  `docs/roadmap.md`.
- Stale docs repaired: `docs/guides/{trace-explorer,graph-explorer}`
  described Studio as "cycle 02 NOT_STARTED" — rewritten against
  `app.routes.ts` reality; `docs/capability-matrix.md` pending items
  1/6/7 moved to fixed (already true in code).

## Wave E — storage/API performance (quality-audit F1/F2/F4/F5)

- **`get_traces` batch read** (`storage/base.py` + `sqlite.py`):
  cohort/window endpoints hydrated N+1 — `/v1/intel/regress` worst
  case ~2001 serialized queries through a single store lock. Now
  one lock acquisition + `IN` queries chunked at 500 ids (SQLite
  999-var limit). Rewired: session graph, eval run/compare, fleet
  efficiency, regress, experiment runs, `correlate_fleet`, alerts
  `_build_window`. Semantics preserved exactly: per-id 404 in
  eval/compare paths; `project_id=None` wildcard picks the newest
  row per id without cross-tenant mixing.
- **`upsert_trace` prefetch**: one `SELECT` per trace replaces the
  per-span PK SELECT; a `delivered` set reproduces the live re-read
  for a repeated `span_id` inside the same envelope — dedup/merge/
  `_union_flags` identical.
- **Pagination 422**: `limit<1`/`offset<0` now reject across
  `traces`/`sessions`/`tools`/`experiments`/runs/session graph —
  previously they clamped silently while sibling params 422'd
  (internal inconsistency). `limit>500` still clamps;
  `admin/audit` already rejected outside 1..500.
- **`write_receipt` OSError** → clean `{"ok": false}` (see Wave A).
- `tests/unit/test_contract_parity.py` already asserted
  `len(routes) >= 50` — vacuity guard pre-existing, no change.

## Dependency security

- `apps/studio/package.json` `overrides.vite ^8.3.5` — dedupes the
  nested `vite@8.3.0` pin via `@angular/build` (Windows arg-handling
  + file-exposure advisories) — `npm audit` → **0 vulnerabilities**;
  build + 97 unit tests verified on the deduped tree.

## CI-fix round (first push exposed three defects)

1. **All 4 Windows legs failed** — the socket guard blocked
   `socket()` construction, but Windows CPython *emulates*
   `socket.socketpair` with a real `socket()` + bind + connect to
   `127.0.0.1`/`::1` (POSIX `socketpair` is a direct syscall that
   never touches `socket()`). Every `TestClient`/anyio self-pipe
   died → 29 errors + 32 failures. Fix: guards moved off
   construction onto `connect`/`connect_ex`/`bind`/`sendto` +
   DNS entrypoints, scoped to non-loopback — strictly more precise
   than the POSIX-only behavior it replaces.
2. **`minimal` job failed** — `test_batch_reads.py` (Wave E)
   imports `TestClient` at module top and was missing from the
   `--ignore` list computed before the wave landed.
3. **`studio-e2e` failed on 1/126** — `graph.spec` zoom/fit read
   `viewBox` with one-shot `getAttribute`, racing Angular's
   signal→attribute flush on CI (vb0 captured pre-fit). Replaced
   with polling `toHaveAttribute` and an explicit assertion that
   fit's canonical camera is `0 0 W H` layout bounds.

## Verification

- `pytest -q` full suite: **green** (~1100 tests incl. benches).
- `ruff check .`, `mypy src` (strict, 80 files): clean.
- Studio: build clean, **97/97** unit; Playwright **126** specs
  verified locally (chromium run: 42/42 ~2min; full 3-browser run
  by Wave C: 126/126 ~7.9min).
- TS SDK: build + **39/39**. `uv build`: wheel + sdist.
- `npm audit` (studio): 0 vulnerabilities.
- CI after this wave: 20 jobs (16 matrix + studio + eval-gate +
  wheel-smoke + minimal + studio-e2e + sdk×2 + audit).

## Still open (deliberate, documented)

- MCP surface expansion (graph/replay/incident/eval/recommendations
  read tools) — bounded follow-up; the observer stays read-only by
  design.
- Python/TS SDK methods for the new surfaces (experiments, intel,
  alerts) — the CLI + API cover the flows; SDK parity tracked in
  `docs/capability-matrix.md`.
- Studio virtual-scroll + server-side cost filter (roadmap
  DEFERRED); installkit scopes/hosts/ledger vendoring (sibling
  parity item 9 — defer until MCP host registration is needed);
  `rc_baseline`/`api_surface` freeze (premature at 0.1.0).
