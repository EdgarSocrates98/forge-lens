# Referência — SDK TypeScript (`@forge-lens/sdk`)

Pacote em `packages/forge-lens-sdk-ts/`. **Zero dependências de runtime**
(fetch nativo, Node ≥18). Testes: `npm test` (tsc + `node:test`).

## `LensTracer`

```ts
const tracer = new LensTracer({
  serviceName: "my-agent",          // obrigatório
  exporter: new HTTPExporter({ endpoint, apiKey, projectId }),
  autoFlush: true,                  // flush ao fechar o span raiz
  sessionId: "sess-1",
  projectId: "default",
  onFlush: (trace) => {},
});
```

### Spans (sync ou async — callback recebe o `Span`)

```ts
tracer.span("name", (s) => {...}, { kind: "tool", attributes, remoteParent });
tracer.agent("researcher", async (s) => { ... });
tracer.llmCall("claude-sonnet-4", (s) => { ... });
tracer.toolCall("web_search", (s) => { ... });
tracer.recordUsage(s, { input_tokens: 10, output_tokens: 4 });
await tracer.flush(traceId?);
```

- Nesting automático via `AsyncLocalStorage` — spans filhos herdam
  `trace_id`/`parent_span_id` do contexto.
- `remoteParent: SpanContext` continua uma trace vinda de headers W3C.
- Exceção no callback → `status=error` + evento `exception`; o erro propaga.

## `HTTPExporter`

```ts
new HTTPExporter({ endpoint?: string; apiKey?: string; projectId?: string;
                   fetchImpl?: typeof fetch });
```

`export(traces, source)` → `POST {endpoint}/v1/traces` com envelope
`TraceIngest` v1. `fetchImpl` injetável (testes/proxies).

## `InMemoryExporter`

`traces: Trace[]` acumulado — testes/captura offline.

## Tipos

`Trace`, `Span`, `SpanEvent`, `SpanLink`, `TraceIngest`, `TraceSource`,
`Status`, `SpanKind`, `Provenance` — espelham `schemas/v1/*.json`.
