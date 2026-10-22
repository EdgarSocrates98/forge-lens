# Referência — SDK Python (`forge_lens.sdk`)

## `LensTracer`

```python
LensTracer(
    service_name="meu-agente",     # obrigatório → TraceSource.system
    exporter=HTTPExporter(...)|InMemoryExporter()|duck-type,
    on_flush=lambda trace: ...,    # callback por trace flushada
    on_export_error=lambda exc, trace: ...,  # falha de auto-flush
    on_dead_letter=lambda trace: ...,        # trace movida a dead_letters
    redact=lambda span: ...,       # hook de redação: por span, no flush,
                                   # antes do snapshot — pode mutar attrs;
                                   # erro do hook conta em export_errors
    auto_flush=True,               # flush quando o span raiz fecha
    session_id=None,
    project_id="default",          # gravado em cada Trace (envelope só preenche "default")
    max_export_failures=5,         # falhas consecutivas antes de dead-letter
    register_atexit=True,          # flush best-effort ao sair do processo
)
```

### Context managers

| Método | SpanKind | Convenções |
|---|---|---|
| `start_span(name, kind, attributes, trace_id, remote_parent)` | qualquer | base de todos |
| `agent(name, **attrs)` | `agent` | run de agente |
| `llm_call(model, provider=...)` | `llm` | `llm.model_name`, `llm.provider` |
| `tool_call(tool_name, arguments=?, capture_args=False)` | `tool` | `tool.parameters` só com `capture_args=True` |

`tracer.record_usage(span, input_tokens=…, output_tokens=…, …)` anexa
`forge_lens.token_usage` (`measurement_source=provider|estimated`).

Erros: exceção dentro do `with` → `status=error` + evento `exception`;
o erro **propaga** (não engolimos).

### Flush

`tracer.flush(trace_id=None)` — envia um snapshot das pendências ao
exporter; retorna o resultado do export ou `None` se vazio. `auto_flush`
dispara ao fechar o span raiz da trace. `tracer.shutdown()` é o flush
terminal para teardown — também roda via `atexit` por padrão
(`register_atexit=False` desliga).

Um `flush()` **mid-flight** (raiz ainda aberta) exporta a trace parcial
e **mantém** o registro: spans seguintes continuam acumulando e o
auto-flush dispara normalmente quando a raiz fecha. O store faz merge
das entregas parciais.

Semântica de falha (observabilidade nunca quebra o observado):

- **Lote preservado**: se `export()` falha, as traces seguem em
  `_pending` — o próximo `flush()` reenvia (nada é perdido).
- **Salvamento de lote envenenado**: se um lote multi-trace falha, cada
  trace é re-exportada individualmente — uma trace rejeitada (ex.: 413
  permanente) não derruba as demais.
- **Dead-letter explícito**: após `max_export_failures` falhas
  consecutivas a trace sai de `_pending` para `tracer.dead_letters` e
  `on_dead_letter` é chamado — nunca descartada silenciosamente.
- **Auto-flush nunca propaga**: exceção do exporter é engolida, contada em
  `tracer.export_errors` e reportada via `on_export_error` — jamais
  mascara a exceção da aplicação instrumentada.
- **`flush()` explícito propaga** o erro do exporter ao chamador
  (mas ainda preserva o lote para retry).
- **Thread-safe**: mutações de `_pending`/`trace.spans`/flush usam
  `threading.RLock`. Callbacks rodam fora do lock.
- **Lock durante o export**: `_lock` cobre o `export()` inteiro — um
  exporter lento bloqueia `start_span` de outras threads (limitado pelo
  timeout do exporter). Apps sensíveis a isso devem usar um exporter
  que retorna rápido e envia em thread própria.

`trace_id` inválido passado a `start_span` não é corrigido
silenciosamente: o span ganha `forge_lens.trace_id_regenerated=true` +
`forge_lens.invalid_trace_id_supplied` e um id novo é gerado.

### Decoradores

```python
@span("nome", kind=SpanKind.INTERNAL, tracer=t)
def f(...): ...

@span("async-op", tracer=t)
async def g(...): ...          # coroutines: o span cobre o await inteiro

@instrument_tool(name="search", capture_args=True, tracer=t)
def search(q): ...
```

Decoradores detectam `async def`, `async def` geradores e geradores
síncronos (`inspect.iscoroutinefunction`/`isasyncgenfunction`/
`isgeneratorfunction`) — o span cobre o corpo real (o `await`, a
iteração), nunca apenas a criação do objeto. `with t.start_span(...)`
também funciona direto dentro de `async def` (contextvars isolam por
task).

Com `capture_args=True`, os argumentos da chamada são gravados em
`tool.parameters` como `{"args": [...], "kwargs": {...}}` (JSON; acima
de 4 KB vira `{"_forge_lens.truncated": true, "_approx_bytes": N}` —
sempre parseável); com `False` nada é capturado.

## `SpanContext` / propagação

`SpanContext.create()`, `.from_headers(headers)` (W3C `traceparent`),
`.to_headers()`. Passe `remote_parent=ctx` a `start_span` para continuar
uma trace remota. `current_span()` lê o topo da pilha (contextvar).

## `HTTPExporter`

```python
HTTPExporter(endpoint="http://127.0.0.1:7777", api_key=None,
             project_id="default", timeout_s=10)
```

`export(traces, source)` → `POST /v1/traces` (stdlib urllib, zero deps de
rede adicionais). `ExportError` em falha HTTP/transporte.

## `InMemoryExporter`

Acumula `traces` — testes, captura offline, export para arquivo. Bounded:
`max_traces=1000` por default com contador `traces_dropped` — é o exporter
default, então um host não-configurado nunca acumula telemetria para sempre.

Retorno de `export()`: `accepted_spans`/`traces` cobrem apenas a telemetria
**retida** (aceite = armazenado, mesma convenção de `IngestResult`);
`submitted_traces`/`traces_dropped` relatam o que foi enviado mas não
retido. `HTTPExporter.export()` devolve a resposta do servidor — aceite do
servidor, não retenção local; os shapes são intencionalmente diferentes.

## `LensClient` — leitura (`forge_lens.sdk.query`)

Lado de leitura do SDK — espelho do `HTTPExporter`: stdlib `urllib`,
zero deps de rede adicionais, todo I/O de rede isolado no módulo.
Cliente fino: cada método mapeia 1:1 para uma rota de consulta `/v1` e
devolve o corpo JSON parseado **verbatim** (dict/list exatamente como a
API emite — nada fabricado nem recalculado localmente).

```python
from forge_lens.sdk import LensClient, QueryError

LensClient(endpoint="http://127.0.0.1:7777", api_key=None,
           project_id="default", timeout_s=10.0)
LensClient.from_config(config=None)   # resolve via load_config() — a
# mesma cadeia FORGE_LENS_* + .forge-lens/config.toml da CLI e do serve
# (endpoint, api_key, project_id)
```

Auth espelha o ingest: `api_key` viaja como `X-Forge-Lens-Key` quando
definida — role `reader` basta. Leituras são abertas no modo local-first
padrão; a chave só é exigida quando o servidor roda `auth_reads` ou
`auth_mode = "keys"`. `project_id` funciona como `--project` da CLI e
`project_id` do `HTTPExporter`: o valor por chamada vence; senão o projeto
configurado no cliente é enviado em toda request (uma key com escopo de
projeto precisa apontar o projeto que ela cobre).

Falhas — transporte, HTTP 4xx/5xx, resposta não-JSON — levantam
`QueryError`, o único tipo de exceção. `exc.status_code` carrega o status
HTTP quando o servidor respondeu (401/403/404/422…) e fica `None` em
falha pura de transporte (conexão recusada, timeout). Um `get_*` de
recurso ausente → `QueryError` com `status_code == 404`.

| Método | Rota | Retorno |
|---|---|---|
| `list_traces(project_id?, session_id?, status?, since?, until?, min_duration_ms?, max_duration_ms?, q?, tool?, model?, agent?, host?, min_total_tokens?, limit=50, offset=0)` | `GET /v1/traces` | `{items, limit, offset}` |
| `get_trace(trace_id, project_id?)` | `GET /v1/traces/{id}` | contrato `Trace` completo |
| `list_sessions(project_id?, limit=100, offset=0)` | `GET /v1/sessions` | `{items, limit, offset}` |
| `list_tools(project_id?, limit=100, offset=0)` | `GET /v1/tools` | `{items, limit, offset}` |
| `trace_graph(trace_id, project_id?)` | `GET /v1/graph/traces/{id}` | `ExecutionGraph` |
| `trace_critical_path(trace_id, project_id?)` | `…/critical-path` | `{path, total_ms, cycles}` |
| `trace_bottlenecks(trace_id, project_id?, top=5)` | `…/bottlenecks` | `{items}` self-time |
| `trace_graph_analysis(trace_id, project_id?)` | `…/analysis` | centralidade/concentração |
| `session_graph(session_id, project_id?, limit=200)` | `GET /v1/graph/sessions/{id}` | grafo de sessão |
| `trace_cost(trace_id, project_id?)` | `GET /v1/traces/{id}/cost` | relatório + `pricing_source` + `waste[]` |
| `trace_failure(trace_id, project_id?)` | `…/failure` | classificação com evidência |
| `trace_replay(trace_id, project_id?)` | `…/replay` | timeline registrado |
| `trace_incident(trace_id, project_id?)` | `…/incident` | bundle versionado |
| `trace_security(trace_id, project_id?)` | `…/security` | observações (observer) |
| `trace_eval(trace_id, project_id?)` | `…/eval` | `{trace_id, results}` |
| `trace_efficiency(trace_id, project_id?)` | `…/efficiency` | tokens/tools/retries |
| `trace_recommendations(trace_id, project_id?)` | `…/recommendations` | recomendações com evidência |
| `trace_correlate(trace_id, project_id?)` | `…/correlate` | co-ocorrências temporais (não-causal) |
| `trace_loops(trace_id, project_id?, repeat_threshold=3)` | `…/loops` | findings heurísticos |
| `list_datasets(project_id?)` | `GET /v1/datasets` | **array puro** (sem envelope) |
| `get_dataset(dataset_id, project_id?)` | `GET /v1/datasets/{id}` | metadados do dataset |
| `list_dataset_versions(dataset_id, project_id?)` | `…/versions` | **array puro** |
| `get_dataset_version(dataset_id, version, project_id?)` | `…/versions/{v}` | versão imutável |
| `health()` | `GET /v1/health` | `{status, version, storage, stats?}` |
| `metrics()` | `GET /v1/metrics` | snapshot de self-observability |
| `capabilities()` | `GET /v1/capabilities` | matriz de capacidades |
| `pricing()` | `GET /v1/analytics/pricing` | tabela de preços efetiva |
| `ops_summary(project_id?)` | `GET /v1/ops/summary` | stats + falhas recentes |
| `list_specialists()` | `GET /v1/analyze/specialists` | `{specialists}` |

Notas:

- Em `list_traces`, `status` ∈ `error|ok|unset`; `since`/`until` aceitam
  ISO-8601 ou `datetime` (naïve lido como UTC — mesma convenção do
  servidor). Todos os filtros combinam com `AND`; validação vive no
  servidor (422 → `QueryError`).
- `/v1/datasets` e `/v1/datasets/{id}/versions` retornam **array puro** —
  as demais listas paginadas usam o envelope `{items, limit, offset}`.
- Rotas `POST` de leitura (argumentos via corpo JSON, ainda sob a
  dependência de auth de leitura):
  - `eval_run(trace_ids, evaluators, dataset?, project_id?)` →
    `POST /v1/eval/run` → `EvalReport` (`results` indexado por trace_id +
    `summary` por evaluator/metric).
  - `eval_compare(a, b, evaluators, gate?, project_id?)` →
    `POST /v1/eval/compare` — `a`/`b` são objetos de lado
    (`{trace_ids, dataset?, project_id?}`); `gate` segue o formato
    `{metrics: {"eval/metric": {min_pass_rate_b, max_drop}},
    fail_on_significant_regression}` → resposta inclui `gate.gate_passed`.
  - `fleet_efficiency(trace_ids?, project_id?)` → `POST /v1/intel/efficiency`
    — roll-up sobre ids explícitos ou a janela recente limitada.
  - `fleet_correlate(project_id?, limit?)` → `POST /v1/intel/correlate` —
    tabelas de co-ocorrência da frota (descritivo, não-causal)
  - `compare_cohorts(a, b, project_id?)` → `POST /v1/intel/regress` —
    deltas observados entre cohorts (significância fica com eval/compare).
  - `run_analysis(specialist, trace_id, budget?, context?, run_id?,
    project_id?)` → `POST /v1/analyze/{specialist}/{trace_id}` — roda um
    specialist registrado sob budget cooperativo.
  - `alerts_evaluate(rules?, use_saved=False, project_id?)` →
    `POST /v1/alerts/evaluate` — regras de threshold contra métricas do
    store em tempo de request.
- Não coberto de propósito: `GET /v1/traces/{id}/stream` (SSE replay —
  stream de eventos, não um recurso JSON) e as rotas de ingest/admin.
- Também sem método hoje (lacuna real, não decisão de design):
  `/v1/experiments*` (specs persistidos de A/B — nem leitura nem
  escrita), os writes `PUT /v1/datasets/*` e `PUT
  /v1/experiments/{id}` (o `LensClient` é read-side; escrita de traces
  é do `HTTPExporter`, não dele), e `POST|DELETE /v1/alerts/rules`
  (admin — `alerts_evaluate` cobre apenas a avaliação). Ver
  `docs/capability-matrix.md`.
