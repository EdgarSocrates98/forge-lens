# @forge-lens/sdk

Zero-dependency TypeScript SDK for Forge Lens — local-first agentic observability.

```ts
import { LensTracer, HTTPExporter } from "@forge-lens/sdk";

const tracer = new LensTracer({
  serviceName: "my-agent",
  exporter: new HTTPExporter({ endpoint: "http://127.0.0.1:7777" }),
});

await tracer.agent("researcher", async () => {
  await tracer.llmCall("claude-sonnet-4", (span) => {
    // ... call the model
    tracer.recordUsage(span, { input_tokens: 200, output_tokens: 80 });
  });
  tracer.toolCall("web_search", () => {
    // ... call the tool
  });
});
```

- Sync and async `fn` supported; nested spans attach automatically via `AsyncLocalStorage`.
- Flushes as `TraceIngest` (contract v1) to `POST /v1/traces`.
- `InMemoryExporter` for offline capture/tests.

## Reading data — `LensClient`

The read side mirrors the write side: fetch-only, zero deps, every
method maps 1:1 onto a `/v1` query route and returns the parsed JSON
verbatim.

```ts
import { LensClient, LensQueryError } from "@forge-lens/sdk";

const client = new LensClient({
  endpoint: "http://127.0.0.1:7777",
  apiKey: "reader-key",       // sent as X-Forge-Lens-Key (optional)
  projectId: "default",       // per-call { projectId } overrides it
  timeoutMs: 10_000,          // AbortController deadline per request
});

const page = await client.listTraces({ status: "error", limit: 20 });
const trace = await client.getTrace(page.items[0].trace_id);
const cost = await client.traceCost(trace.trace_id);
const loops = await client.traceLoops(trace.trace_id, { repeatThreshold: 3 });
```

- Auth mirrors ingest: `apiKey` travels as `X-Forge-Lens-Key` (a
  `reader` role suffices; reads are open in default local-first mode).
- Failures — transport, HTTP 4xx/5xx, timeout, non-JSON — throw
  `LensQueryError`; `err.statusCode` carries the HTTP status when the
  server answered (404 for a missing trace) and stays `undefined` on
  pure transport failure.
- `listTraces`/`listSessions`/`listTools` return the `{items, limit,
  offset}` envelope; `listDatasets`/`listDatasetVersions` return bare
  arrays (those routes have no envelope).
- Read-side POSTs are covered too: `evalRun`, `evalCompare`,
  `fleetEfficiency`, `compareCohorts`, `runAnalysis`, `alertsEvaluate`.
- Not covered on purpose: `GET /v1/traces/{id}/stream` (SSE) and the
  ingest/admin routes. See `docs/reference/sdk-typescript.md` for the
  full method table.
