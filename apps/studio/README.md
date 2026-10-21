# Studio

This project was generated using [Angular CLI](https://github.com/angular/angular-cli) version 22.2.3.

## Development server

To start a local development server, run:

```bash
ng serve
```

Once the server is running, open your browser and navigate to `http://localhost:4200/`. The application will automatically reload whenever you modify any of the source files.

## Code scaffolding

Angular CLI includes powerful code scaffolding tools. To generate a new component, run:

```bash
ng generate component component-name
```

For a complete list of available schematics (such as `components`, `directives`, or `pipes`), run:

```bash
ng generate --help
```

## Building

To build the project run:

```bash
ng build
```

This will compile your project and store the build artifacts in the `dist/` directory. By default, the production build optimizes your application for performance and speed.

## Running unit tests

To execute unit tests with the [Vitest](https://vitest.dev/) test runner, use the following command:

```bash
ng test
```

## Running end-to-end tests

For end-to-end (e2e) testing, run:

```bash
ng e2e
```

Angular CLI does not come with an end-to-end testing framework by default. You can choose one that suits your needs.

## Additional Resources

For more information on using the Angular CLI, including detailed command references, visit the [Angular CLI Overview and Command Reference](https://angular.dev/tools/cli) page.

## Trace Explorer filters & Sessions

The Trace Explorer (`/`, also reachable as `/traces`) mirrors every filter
into the URL query string, so filtered views are deep-linkable. Beyond the
status select it maps these `GET /v1/traces` params: `q` (trace id or
span-name substring), `tool` (span `tool.name`), `session_id`,
`min_duration_ms`/`max_duration_ms`, and `since`/`until` (picked as local
datetimes, sent as ISO instants). **Save view** persists the whole filter
set to `localStorage` (`lens-view:*`); older saved views missing the new
keys still load.

The **sessions** view (`/sessions`, linked in the header) lists sessions
from `GET /v1/sessions` in pages of 50 with offset-based "load more"
pagination. The search box filters fetched rows by session-id prefix, and
each session links to `/traces?session_id=<id>`.

The **datasets** view (`/datasets`, linked in the header) manages eval
datasets and compares cohorts. It lists `GET /v1/datasets` (a bare array —
id, name, task, created), filters client-side by id/name, and a **new
dataset** panel `PUT`s `{name, task?}` to `/v1/datasets/{id}` (an existing
id upserts its metadata). Selecting a row loads `GET /v1/datasets/{id}` plus
`GET …/versions` (version, description, case_count, created). The **new
version** form `PUT`s `{cases, description?}` to `…/versions/{v}` — each
case row takes a `case_id` plus optional `input`/`expected` JSON objects;
versions are append-only so a repeat is a surfaced 409. The **compare
cohorts** panel posts `POST /v1/eval/compare` with
`{a: {trace_ids, dataset?}, b: {…}, evaluators: [{name}], gate?}` — both
sides need stored trace ids (a missing id is a 404) and a side's `dataset`
is only a report label. Results render per-metric `n`, means, delta, pass
rates, significance and p-value, plus the gate verdict/failures when a gate
was sent; `null`s render as `n/a`. Below the deltas, a **failure examples**
section recomputes per-trace results (`POST /v1/eval/run` over both
cohorts, same evaluator specs — deterministic) and lists every `score: 0`
result with its reason and a link to the trace; and when both sides carry
means for a quality metric *and* a cost/token metric (matched on the
`evaluator/metric` name), a small **quality vs cost/token** table compares
the per-side means — it renders only when both classes exist.

The **experiments** view (`/experiments`, linked in the header and from
`/datasets`) manages persisted A/B specs. It lists `GET /v1/experiments`
(id, name, status, `result_ref`, created) and a **new experiment** panel
`PUT`s `{name, description?, dataset_id?, dataset_version?, cohorts,
evaluators}` to `/v1/experiments/{id}` — each cohort pins `trace_ids`
(1–500) plus optional `label`/`config` labels, the dataset fields pin the
truth the experiment evaluates against (a `dataset_version` without
`dataset_id` is rejected client-side, matching the server's 422), and
evaluators store as `{name}` specs. Selecting a row loads
`GET /v1/experiments/{id}` — the spec plus the append-only run history
(newest first: run id, timestamp, gate verdict, delta summary). **Run
now** posts `POST /v1/experiments/{id}/run` (optional significance gate),
prepends the recorded run, and renders its report through the same view
as compare — deltas, gate failures, failure examples recomputed with the
stored spec's evaluators (a run predating a re-spec is labeled as such),
and the quality-vs-cost table when the metric classes allow it.

The trace detail **evidence** tab ends with an **eval** panel that auto-loads `GET /v1/traces/:id/eval` and can run any of the built-in evaluators via `POST /v1/eval/run` (`score: null` renders as `n/a` with the evaluator's reason). Checking a param-taking evaluator reveals its inputs — `*` marks params the evaluator requires, `allowed` takes a comma-separated list, `expected` accepts a JSON value, and a malformed value blocks the run before POST (an omitted param instead comes back as an `evaluator_error` row). A collapsible **replay** panel below it renders `GET /v1/traces/:id/replay` — the recorded span/event timeline in server order (offset, kind, name, event, status), capped at 200 rows behind a show-more button, with honest empty/error states. A controls bar steps through the loaded entries (no SSE): `|«`/`»|` jump to start/end, `‹`/`›` step, play/pause auto-advances every `1000ms ÷ speed` (0.5×–4× select) and stops at the end, and `←`/`→`/space work while the panel has focus. The current row is highlighted and scrolled into view; stepping past the render cap expands it. Jump-to buttons land on the next `status: error` entry, the next entry belonging to a `kind: 'tool'` span (from the trace's own spans), or the next `kind: 'event'` — disabled when the timeline has none.

The **operations** view (`/ops`, linked in the header) renders `GET /v1/ops/summary`: the store `stats` map as cards (traces, spans, error spans, db size, schema — extra keys a store adds still render), the bounded-window trace/failure counts, and the recent-failure trace ids linked to `/traces/{id}`; the payload's own `bounded window (200 traces)` note is shown verbatim. Below it, an **alerts** panel posts `POST /v1/alerts/evaluate` with `{use_saved: true}` and lists the fired rules (metric, op, threshold, observed, severity, message). The response only reports fired rules plus an `evaluated` count — there is no rules-listing GET, so passing rules are summarized ("none fired"), never rowed; rule management stays on `POST`/`DELETE /v1/alerts/rules`. A manual **refresh** button re-issues both calls — nothing polls.

## API connection

The Studio talks to `forge-lens serve` (default `http://localhost:7777`).
Use the ⚙ settings control in the header (or `localStorage` keys
`lens-api-base` / `lens-api-key`) to point at a different base URL or to
set the `X-Forge-Lens-Key` header when the server runs `auth_mode` of
`key`/`keys`. During development, `ng serve --proxy-config proxy.conf.json`
forwards `/v1` to `:7777` so no CORS setup is needed.

Note: the API key is stored in `localStorage`, which is readable by any
script running in the page — acceptable for a local-first tool bound to
loopback, but do not reuse a high-value key here if Studio is ever
served from a shared origin.

The **graph** view (`/graph`, deep-linkable via `?trace_id=`/`?session_id=`) renders `GET /v1/graph/…` responses as a hand-rolled SVG DAG — longest-path layered layout, edge-kind strokes (parent solid / sequence dashed / link dotted, dangling edges as stubs), drag-pan + zoom/fit controls, a critical-path highlight toggle, a self-time bottlenecks table, and a 500-node cap with an honest truncation banner.
