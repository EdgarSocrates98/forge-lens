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
  onExportError: (err, trace) => {}, // falha de auto-flush
  onDeadLetter: (trace) => {},       // trace movida a deadLetters
  redact: (span) => {},              // hook de redação: por span, no flush,
                                     // antes do snapshot — pode mutar attrs;
                                     // erro do hook conta em exportErrors
  maxExportFailures: 5,              // falhas consecutivas antes de dead-letter
  shutdownHook: true,                // flush best-effort no beforeExit
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

Um `flush()` **mid-flight** (raiz ainda aberta) exporta um snapshot da
trace parcial e **mantém** o registro — o auto-flush dispara normalmente
quando a raiz fecha; o store faz merge das entregas parciais.
`await tracer.shutdown()` é o flush terminal para teardown — também roda
best-effort no `beforeExit` (`shutdownHook: false` desliga).

Semântica de falha (observabilidade nunca quebra o observado):

- `flush()` **preserva o lote** em `pending` quando `export()` rejeita —
  retry manual reenvia; o erro propaga só para o chamador explícito.
- **Lote envenenado**: se um lote multi-trace falha, cada trace é
  re-exportada sozinha — uma rejeição permanente não derruba as demais.
- **Dead-letter explícito**: após `maxExportFailures` falhas consecutivas
  a trace vai para `tracer.deadLetters` e `onDeadLetter` é chamado.
- Auto-flush é fire-and-forget **com `.catch()`**: rejeição é contada em
  `tracer.exportErrors` e reportada via `onExportError` — nunca vira
  *unhandled promise rejection* no processo hospedeiro.
- Flushes concorrentes são serializados (fila interna); snapshots usam
  `structuredClone` — mutação posterior não vaza para o lote em voo.
- `traceId` inválido no contexto não é corrigido silenciosamente: o span
  ganha `forge_lens.trace_id_regenerated` +
  `forge_lens.invalid_trace_id_supplied` e um id novo é gerado.

## `HTTPExporter`

```ts
new HTTPExporter({ endpoint?: string; apiKey?: string; projectId?: string;
                   fetchImpl?: typeof fetch });
```

`export(traces, source)` → `POST {endpoint}/v1/traces` com envelope
`TraceIngest` v1. `fetchImpl` injetável (testes/proxies).

## `InMemoryExporter`

`traces: Trace[]` acumulado — testes/captura offline. Bounded:
`maxTraces=1000` por default com contador `tracesDropped` (não-finito →
`TypeError`, como o `int()` do SDK Python).

Retorno de `export()`: `accepted_spans`/`traces` cobrem apenas a telemetria
**retida**; `submitted_traces`/`traces_dropped` relatam o que foi enviado
mas não retido. `HTTPExporter.export()` devolve a resposta do servidor —
aceite do servidor, não retenção local; os shapes são intencionalmente
diferentes.

## Tipos

`Trace`, `Span`, `SpanEvent`, `SpanLink`, `TraceIngest`, `TraceSource`,
`Status`, `SpanKind`, `Provenance` — espelham `schemas/v1/*.json`.
