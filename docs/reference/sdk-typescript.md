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

## `LensClient` — leitura (`src/query.ts`)

Lado de leitura do SDK — espelho do `HTTPExporter`: fetch nativo, zero
deps de rede adicionais, todo I/O de rede isolado no módulo. Cliente
fino: cada método mapeia 1:1 para uma rota de consulta `/v1` e devolve
o corpo JSON parseado **verbatim** — nada fabricado nem recalculado
localmente. Espelha o `LensClient` do SDK Python
(`forge_lens.sdk.query`) método a método.

```ts
import { LensClient, LensQueryError } from "@forge-lens/sdk";

new LensClient({
  endpoint: "http://127.0.0.1:7777",  // barras finais removidas
  apiKey: undefined,                  // vai como X-Forge-Lens-Key
  projectId: "default",
  timeoutMs: 10_000,                  // deadline via AbortController
  fetchImpl: fetch,                   // injetável (testes/proxies)
});
```

Auth espelha o ingest: `apiKey` viaja como `X-Forge-Lens-Key` quando
definida — role `reader` basta. Leituras são abertas no modo local-first
padrão; a chave só é exigida quando o servidor roda `auth_reads` ou
`auth_mode = "keys"`. `projectId` funciona como `--project` da CLI e
`projectId` do `HTTPExporter`: o valor por chamada vence; senão o projeto
configurado no cliente é enviado em toda request.

Falhas — transporte, HTTP 4xx/5xx, timeout, resposta não-JSON — levantam
`LensQueryError`, o único tipo de exceção. `err.statusCode` carrega o
status HTTP quando o servidor respondeu (401/403/404/422…) e fica
`undefined` em falha pura de transporte (conexão recusada, timeout). Um
`get*` de recurso ausente → `LensQueryError` com `statusCode === 404`.

| Método | Rota | Retorno |
|---|---|---|
| `listTraces({projectId?, sessionId?, status?, since?, until?, minDurationMs?, maxDurationMs?, q?, tool?, model?, agent?, host?, minTotalTokens?, limit=50, offset=0})` | `GET /v1/traces` | `{items, limit, offset}` |
| `getTrace(traceId, {projectId?})` | `GET /v1/traces/{id}` | contrato `Trace` completo |
| `listSessions({projectId?, limit=100, offset=0})` | `GET /v1/sessions` | `{items, limit, offset}` |
| `listTools({projectId?, limit=100, offset=0})` | `GET /v1/tools` | `{items, limit, offset}` |
| `traceGraph(traceId, {projectId?})` | `GET /v1/graph/traces/{id}` | `ExecutionGraph` |
| `traceCriticalPath(traceId, {projectId?})` | `…/critical-path` | `{path, total_ms, cycles}` |
| `traceBottlenecks(traceId, {projectId?, top=5})` | `…/bottlenecks` | `{items}` self-time |
| `traceGraphAnalysis(traceId, {projectId?})` | `…/analysis` | centralidade/concentração |
| `sessionGraph(sessionId, {projectId?, limit=200})` | `GET /v1/graph/sessions/{id}` | grafo de sessão |
| `traceCost(traceId, {projectId?})` | `GET /v1/traces/{id}/cost` | relatório + `pricing_source` + `waste[]` |
| `traceFailure(traceId, {projectId?})` | `…/failure` | classificação com evidência |
| `traceReplay(traceId, {projectId?})` | `…/replay` | timeline registrado |
| `traceIncident(traceId, {projectId?})` | `…/incident` | bundle versionado |
| `traceSecurity(traceId, {projectId?})` | `…/security` | observações (observer) |
| `traceEval(traceId, {projectId?})` | `…/eval` | `{trace_id, results}` |
| `traceEfficiency(traceId, {projectId?})` | `…/efficiency` | tokens/tools/retries |
| `traceRecommendations(traceId, {projectId?})` | `…/recommendations` | recomendações com evidência |
| `traceCorrelate(traceId, {projectId?})` | `…/correlate` | co-ocorrências temporais (não-causal) |
| `traceLoops(traceId, {projectId?, repeatThreshold=3})` | `…/loops` | findings heurísticos |
| `listDatasets({projectId?})` | `GET /v1/datasets` | **array puro** (sem envelope) |
| `getDataset(datasetId, {projectId?})` | `GET /v1/datasets/{id}` | metadados do dataset |
| `listDatasetVersions(datasetId, {projectId?})` | `…/versions` | **array puro** |
| `getDatasetVersion(datasetId, version, {projectId?})` | `…/versions/{v}` | versão imutável |
| `health()` | `GET /v1/health` | `{status, version, storage, stats?}` |
| `metrics()` | `GET /v1/metrics` | snapshot de self-observability |
| `capabilities()` | `GET /v1/capabilities` | matriz de capacidades |
| `pricing()` | `GET /v1/analytics/pricing` | tabela de preços efetiva |
| `opsSummary({projectId?})` | `GET /v1/ops/summary` | stats + falhas recentes |
| `listSpecialists()` | `GET /v1/analyze/specialists` | `{specialists}` |

Notas:

- Em `listTraces`, `status` ∈ `error|ok|unset` (`StatusCode`);
  `since`/`until` aceitam ISO-8601 ou `Date` (serializado via
  `toISOString()`). Todos os filtros combinam com `AND`; validação vive
  no servidor (422 → `LensQueryError`).
- `/v1/datasets` e `/v1/datasets/{id}/versions` retornam **array puro** —
  as demais listas paginadas usam o envelope `{items, limit, offset}`.
- Rotas `POST` de leitura (argumentos via corpo JSON, ainda sob a
  dependência de auth de leitura):
  - `evalRun(traceIds, evaluators, {dataset?, projectId?})` →
    `POST /v1/eval/run` → `EvalReport` (`results` indexado por trace_id +
    `summary` por evaluator/metric).
  - `evalCompare(a, b, evaluators, {gate?, projectId?})` →
    `POST /v1/eval/compare` — `a`/`b` são objetos de lado
    (`{trace_ids, dataset?, project_id?}`); `gate` segue o formato
    `{metrics: {"eval/metric": {min_pass_rate_b, max_drop}},
    fail_on_significant_regression}` → resposta inclui `gate.gate_passed`.
  - `fleetEfficiency(traceIds?, {projectId?})` → `POST /v1/intel/efficiency`
    — roll-up sobre ids explícitos ou a janela recente limitada.
  - `fleetCorrelate({projectId?, limit?})` → `POST /v1/intel/correlate` —
    tabelas de co-ocorrência da frota (descritivo, não-causal).
  - `compareCohorts(a, b, {projectId?})` → `POST /v1/intel/regress` —
    deltas observados entre cohorts (significância fica com eval/compare).
  - `runAnalysis(specialist, traceId, {budget?, context?, runId?,
    projectId?})` → `POST /v1/analyze/{specialist}/{trace_id}` — roda um
    specialist registrado sob budget cooperativo.
  - `alertsEvaluate(rules?, {useSaved?, projectId?})` →
    `POST /v1/alerts/evaluate` — regras de threshold contra métricas do
    store em tempo de request.
- O timeout é do cliente, não do transporte: o request corre contra um
  deadline por `Promise.race` e o `AbortController` cancela o fetch real
  — um `fetchImpl` que ignora `signal` ainda estoura `LensQueryError` no
  prazo.
- Não coberto de propósito: `GET /v1/traces/{id}/stream` (SSE replay —
  stream de eventos, não um recurso JSON) e as rotas de ingest/admin.
- Sem `fromConfig`: o SDK TS não tem cadeia `FORGE_LENS_*`/config.toml —
  `endpoint`/`apiKey`/`projectId` vêm do construtor.

## Tipos

`Trace`, `Span`, `SpanEvent`, `SpanLink`, `TraceIngest`, `TraceSource`,
`Status`, `SpanKind`, `Provenance` — espelham `schemas/v1/*.json`.

Do lado de leitura: `JsonObject`, `ListEnvelope<T>` (`{items, limit,
offset}`), `HealthResponse`, `TraceEvalResponse`, `SpecialistsResponse`,
`EvalSide`, `CohortSide`, `ListTracesFilters`, `ListPageOptions`,
`ProjectScoped`, `LensClientOptions`.
