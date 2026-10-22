# Studio E2E (Playwright) — homologation suite

Real end-to-end coverage for the Angular Studio: a deterministic dataset
is seeded into a fresh SQLite store, `forge-lens serve` binds it on a
dedicated port, the **production build** of the Studio is served
statically with a same-origin `/v1` proxy, and Playwright drives real
browsers against the rendered UI. There are no network mocks for the
happy paths — every list, filter, graph, eval and error state is backed
by the API's actual responses (a handful of *error-state* specs stub
single endpoints via `page.route` to simulate empty lists / API down —
that is the only interception in the suite).

## Layout

```
apps/studio/
  playwright.config.ts        # projects ×3, webServer wiring
  e2e/
    fixtures.ts               # shared test fixture (API base → same-origin /v1)
    seed.ts                   # typed reader for the seed manifest
    serve-dist.mjs            # zero-dep static server + /v1 proxy
    *.spec.ts                 # the suites (see coverage map below)
    .runtime/                 # seeded db + manifest (generated, git-ignored)
    .artifacts/               # playwright report/traces/videos (git-ignored)
scripts/seed_demo_data.py     # deterministic seeder (runs inside .venv)
docs/studio-e2e.md            # this file
```

## Prereqs

```bash
# repo root — the seeder + `forge-lens serve` come from the venv
python -m venv .venv && .venv/bin/pip install -e .   # once

cd apps/studio
npm install                # installs @playwright/test
npx playwright install     # browser binaries: chromium, firefox, webkit
```

## Run

```bash
cd apps/studio

npm run e2e:chromium                  # fastest loop — required locally
npx playwright test --project=firefox # per-browser matrix legs
npx playwright test --project=webkit
npm run e2e                           # full matrix (all 3 projects, 120 tests)

npm run e2e:report                    # open the last HTML report
```

Ports are fixed by `playwright.config.ts`: API on `127.0.0.1:7778`,
Studio on `127.0.0.1:4230`. They deliberately avoid the dev defaults
(7777/4200) so a developer's own `forge-lens serve` / `ng serve` never
collides with a test run.

### What the `webServer` block does per run

1. `scripts/seed_demo_data.py` rebuilds `e2e/.runtime/lens.db` from
   scratch (sha256-derived ids, frozen clock) and writes
   `e2e/.runtime/seed-manifest.json` — the specs read the manifest
   (`e2e/seed.ts`) and never hard-code trace ids.
2. `forge-lens serve --db <seeded> --port 7778` starts the real API;
   Playwright waits on `/v1/health`.
3. `npm run build` produces `dist/studio/browser`; `e2e/serve-dist.mjs`
   serves it on :4230 with an SPA fallback and proxies `/v1/*` to the
   API on :7778 — the API ships **no CORS headers**, so same-origin
   proxying is the documented path (see `apps/studio/README.md` →
   "API connection"). The dev server is intentionally *not* used: its
   watcher exhausts the host inotify limit (ENOSPC) in this
   environment, and a static build is what users actually run.
4. The shared fixture (`e2e/fixtures.ts`) sets
   `localStorage['lens-api-base'] = '/v1'` before the app boots —
   overriding the built-in `http://localhost:7777/v1` default.

### Seeded data (deterministic)

10 traces / 28 spans through the real `IngestPipeline`:

- sessions `session-demo-alpha` (3 traces), `session-demo-beta` (2);
- a failing trace (`status=error`, exception event, tool error);
- tool calls (`web-search`, `calculator`, `retriever`), token usage on
  llm spans, span events, an `unset`-status trace, a slow trace;
- dataset `ds-demo-qa@v1` (2 cases) and experiment `exp-demo-ab`
  (cohort A = clean traces, cohort B includes the failing trace;
  evaluators `error_free`, `tool_success`).

Ids are sha256-stable; reseeding an empty db reproduces identical rows.

## Coverage map

| spec | covers |
|---|---|
| `smoke.spec.ts` | app boot + nav; dark↔light theme toggle + `lens-theme` persistence across reload; ⚙ API-settings panel shows the wired base; Tab keyboard nav with visible focus |
| `traces.spec.ts` | trace list newest-first; status filter + URL query sync + deep link; text search (`q` matches span names); tool/session/duration/`since` filters; `limit` server-side paging + column sort; saved views round-trip (localStorage); export-JSON download; row → `/traces/:id` deep link |
| `trace-detail.spec.ts` | waterfall hierarchy + error styling; span selection evidence panel; evidence tab (intel, eval battery, replay); replay controls — step, jump-to-start/end/error/event, ArrowLeft/Right keys, play auto-advance + pause freeze; eval-params form validation; unknown id → 404 banner |
| `sessions.spec.ts` | session list ordering; client-side prefix filter; row → filtered trace list deep link |
| `graph.spec.ts` | `/graph?trace_id=` DAG (nodes, edges, server statline); node selection → aside evidence + trace-detail link; bottlenecks table row-select; critical-path toggle; zoom + fit viewBox; **620-node wide trace**: truncation banner + exactly-500 drawn, `find` reveals/centers a beyond-cap node (hit-marked, tail evicted); **3 200-node trace** (route-intercepted synthetic payload — §14 cenário F: real ≥3k navigation): `find` reveals/centers `n3199` beyond the cap; `/graph?session_id=` mode (sequence edges); subject form → URL sync; unknown id → error banner |
| `eval.spec.ts` | dataset list/detail/versions; `/v1/eval/compare` deltas + failure-examples table (failing trace shows score-0 rows); compare validation on unknown ids; dataset create form → real `PUT` upsert; experiment spec render + `run now` appends a run; empty-cohort client-side rejection |
| `ops.spec.ts` | ops summary stat cards, recent failures, alert evaluation; capabilities host-matrix table |
| `states.spec.ts` | empty trace list; API-down unreachable banner; sessions server-error surfaced verbatim; unknown-trace deep link; empty datasets/experiments create affordances; `/graph` without subject → entry hint |

## Matrix results

Environment: Node v22.23.2, npm 10.9.8, `@playwright/test` 1.64.1,
Linux x86_64. Browser builds: `chromium-1248`, `firefox-1555`,
`webkit-2370`. Date: 2026-10-22.

| project | tests | result | wall time | command |
|---|---|---|---|---|
| chromium | 40 | **40 passed** | ~2.1 min | `npx playwright test --project=chromium` |
| firefox  | 40 | **40 passed** | ~3.6 min | `npx playwright test --project=firefox` |
| webkit   | 40 | **40 passed** | ~3.1 min | `npx playwright test --project=webkit` |
| **all 3, one invocation** | 120 | **120 passed** | ~6.5 min | `npx playwright test` |

No browser-specific failures. Firefox/WebKit execute ~3-4× slower than
Chromium on this box, so the config uses `timeout: 90s` /
`expect: 15s` globally rather than per-project forks — with a
single-project `30s` default they were hitting test timeouts
mid-assertion.

Note on shared state: one `npx playwright test` invocation shares the
single seeded db across all three projects, so mutation specs are
written state-agnostically (e.g. "run now" asserts the run history
*grows*, not that it starts empty).

Artifacts land in `e2e/.artifacts/` (`report/` HTML report,
`test-results/` failure screenshots+videos+traces, `results.json`).
Both `.artifacts/` and `.runtime/` are git-ignored via
`e2e/.gitignore`; the root `.gitignore` also covers the default
Playwright output dirs for out-of-config runs.

## Bugs encontrados

None in `apps/studio/src/app/**`. Every red test during development
traced back to a spec-side issue (strict-mode locator collisions where
a `<details>` panel keeps hidden-but-present duplicates; a test-side
race in the 1s replay tick; an inverted expectation on the theme
button, which labels the *target* theme; zero-height SVG `<line>`
edges being invisible to `toBeVisible`). The application rendered the
seeded data, surfaced API errors verbatim, and held URL state correctly
under all three engines.

Two environment notes (not app bugs):

- `ng serve` is unusable in this environment — its watcher hits the
  host inotify limit (ENOSPC). The suite serves the production build
  instead, which is also the shipped artifact.
- `POST /v1/eval/compare` with unknown trace ids returns `404` — the
  suite asserts that as the correct validation behavior.
