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
