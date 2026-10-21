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

The trace detail **evidence** tab ends with an **eval** panel that auto-loads `GET /v1/traces/:id/eval` and can run any of the built-in evaluators via `POST /v1/eval/run` (`score: null` renders as `n/a` with the evaluator's reason). Checking a param-taking evaluator reveals its inputs — `*` marks params the evaluator requires, `allowed` takes a comma-separated list, `expected` accepts a JSON value, and a malformed value blocks the run before POST (an omitted param instead comes back as an `evaluator_error` row).

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
