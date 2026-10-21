# Referência — API HTTP (v1)

Base: `http://127.0.0.1:7777/v1`. Todos os endpoints retornam JSON —
exceção: `GET /v1/traces/{id}/stream` responde `text/event-stream`
(SSE). `GET /favicon.ico` existe fora do `/v1` (204, fora do schema).

## Auth

Três modos (`auth_mode` no TOML / `FORGE_LENS_AUTH_MODE`; o default é
`key` quando `api_key` está configurada, senão `none`):

- `none` — modelo local single-user: a identidade resolve como admin
  sem chave; nenhuma rota pede credencial.
- `key` — o segredo compartilhado em `X-Forge-Lens-Key` resolve como
  admin. `auth_mode="key"` sem `api_key` é erro de configuração (o app
  não sobe).
- `keys` — chaves gerenciadas (`POST /v1/admin/keys`), persistidas só
  como hash SHA-256. Cada chave tem role `reader` ⊂ `writer` ⊂ `admin`
  e `projects[]`: lista vazia = *unscoped* (todos os projetos);
  preenchida = a chave só toca aqueles `project_id`s — leitura e
  escrita.

Nível exigido por rota:

- **writer** (`check_auth`): `POST /v1/traces`, `POST /v1/otlp/traces`,
  `PUT /v1/datasets/*`.
- **reader** (`read_deps`): todos os GETs de dados **e** os POSTs de
  consulta (`/v1/eval/*`, `/v1/intel/*`, `/v1/analyze/*`,
  `/v1/alerts/evaluate`) — exigido quando `auth_reads = true`
  (`FORGE_LENS_AUTH_READS`) ou sempre em modo `keys`; fora disso a
  leitura é aberta.
- **admin** (`admin_deps`): `/v1/admin/*`, `POST|DELETE
  /v1/alerts/rules*`. Em `none`/`key` a identidade já é admin.

Erros de auth: `401` chave ausente/inválida; `403` role insuficiente
ou `project_id` fora do escopo. `project_id` omitido resolve para
`cfg.project_id` na checagem **e** na query — uma chave com escopo não
vira leitura global pelo parâmetro omitido.

Erros comuns: `400` JSON inválido no corpo · `404` recurso inexistente
· `409` dataset version já existe (append-only) · `413` corpo acima de
`max_payload_bytes` (default 8 MiB — streaming aborta no primeiro byte
excedente) · `415` OTLP protobuf · `422` parâmetro/corpo malformado ·
`429` rate limit de **ingestão** (`rate_limit_per_minute`, default 0 =
desligado — não se aplica a consultas).

Convenções de resposta: as listas paginadas `traces`/`sessions`/`tools`
retornam envelope `{items, limit, offset}` (`limit` clampa a 1..500,
`offset` clampa a ≥0 — e todo parâmetro inteiro é limitado a int64:
`offset`/`min_total_tokens` > `2^63−1` → `422`, nunca um bind no
driver); `datasets`, dataset versions, `admin/keys`
e `admin/audit` retornam **array puro**; `graph/.../bottlenecks`
retorna `{items}` sem envelope; o restante retorna objeto.

## Ingestão

O corpo é lido em streaming com limite real (`max_payload_bytes`) — um
POST chunked/sem `Content-Length` aborta no primeiro byte acima do
teto; o check de `Content-Length` é apenas fast-path. `warnings[]` é
limitado a 200 entradas + nota de overflow — payloads adversariais não
amplificam a resposta.

### `POST /v1/traces`
Envelope nativo `TraceIngest` (`schemas/v1/trace-ingest.json`).
`project_id` do envelope é aplicado às traces.
Resposta: `{accepted_spans, updated_spans, duplicate_spans, traces[],
sanitize{}, warnings[]}`.
Erros: `413` payload/limite excedido, `422` envelope inválido ou span com
`trace_id` divergente, `429` rate limit.

`updated_spans` conta re-entregas **alteradas** mescladas no store
(ex.: `end_time` tardio); `duplicate_spans` conta re-entregas idênticas.

### `POST /v1/otlp/traces`
`ExportTraceServiceRequest` **JSON** (OTLP/HTTP). Protobuf não é aceito.
Resposta: `{accepted_spans, updated_spans, duplicate_spans, traces[],
sanitize{}, warnings[]}` — spans malformados são *quarantined*: descartados com
motivo listado em `warnings`, enquanto os válidos são ingeridos.
Erros: `400` JSON inválido, `413` payload/limite excedido, `415`
Content-Type protobuf/octet-stream, `422` forma OTLP
inválida, `429` rate limit.

## Consulta

### `GET /v1/traces?project_id&session_id&status&since&until&min_duration_ms&max_duration_ms&q&tool&model&agent&host&min_total_tokens&limit≤500&offset`
`{items:[{trace_id, project_id, session_id, root_span_id, started_at,
ended_at, status, span_count, error_count}], limit, offset}`.

Todos os filtros combinam com `AND`; parâmetros malformados → `422`:

- `status`: um de `ok|error|unset` (coluna `status_code`).
- `session_id`: correspondência exata.
- `since`/`until`: ISO-8601 → limites **inclusivos** sobre `started_at`
  (timestamps naïve são lidos como UTC).
- `min_duration_ms`/`max_duration_ms`: números ≥ 0, comparados contra
  `ended_at − started_at`. Traces sem `ended_at` (ainda em execução)
  **são excluídas sempre que um filtro de duração é usado** — duração
  desconhecida não é coagida a 0 nem a "ainda rodando".
- `q`: substring literal sobre `trace_id` OU nome de qualquer span.
- `tool`: substring literal sobre o valor do atributo `tool.name` de
  qualquer span. `q` e `tool` escapam `%`/`_`/`\` — um `%` no termo
  casa um sinal de porcentagem literal, nunca funciona como wildcard.
- `model`: substring literal sobre os atributos `gen_ai.request.model`
  ou `gen_ai.response.model` de qualquer span.
- `agent`: substring literal sobre `gen_ai.agent.name` de qualquer span,
  OU sobre a identidade do produtor (`source.system`, ou o atributo de
  recurso `service.name`).
- `host`: substring literal sobre `source.host` ou o atributo de recurso
  `host.name`.
- `min_total_tokens`: inteiro em `0..2^63−1` — soma de
  `gen_ai.usage.total_tokens` sobre todos os spans da trace ≥ limite.
  Traces sem **nenhum** span com observação de uso nunca casam —
  inclusive no limite `0` (ausência de evidência ≠ zero observado).
  Não existe filtro `cost` — custo é derivado do
  pricing registry na leitura, nunca um predicado persistido (§02.6:
  filtrar SQL por custo exigiria materializar um valor derivado).

### `GET /v1/sessions?project_id&limit≤500&offset`
Roll-up por sessão — `{items:[{session_id, traces, spans,
first_started_at, last_activity_at}], limit, offset}` (default
`limit=100`).
`first_started_at` = menor `started_at` da sessão; `last_activity_at` =
maior instante observável (o `ended_at` de uma trace encerrada, ou o
`started_at` de uma ainda em execução — nunca `null` enquanto houver
traces). Ordenação determinística: `last_activity_at` DESC, desempate
por `session_id` ASC. Escopo resolvido como nas outras rotas: sem
`project_id`, usa `cfg.project_id`; chaves com escopo recebem `403`
fora dele.

### `GET /v1/tools?project_id&limit≤500&offset`
Roll-up de ferramentas sobre spans `kind=tool` (default `limit=100`) —
`{items:[{tool, calls, traces, errors, avg_duration_ms}], limit,
offset}`. Uma linha por valor do atributo `tool.name`; spans tool sem o
atributo caem em `(unnamed)` — nunca desaparecem da contagem.
Ordenação: `calls` DESC, desempate por nome ASC. Mesmo escopo das
demais rotas.

### `GET /v1/traces/{trace_id}?project_id`
Contrato `Trace` completo ou `404`. Sem `project_id`, usa o
`cfg.project_id` configurado — a leitura cruzada entre projetos existe
apenas no `Store` interno (modelo local single-user — `project_id`
particiona dados, não é fronteira de segurança; em modo `keys` a
fronteira é o `projects[]` da chave).

## Datasets (Cycle 06)

Datasets são metadados mutáveis (upsert atualiza `name`/`task`/
`metadata`); **versions são append-only** — nunca mutadas in-place.
Escrita exige role `writer`+; `created_at` é dado opcional do cliente
(ISO-8601) — a API não inventa timestamp de criação (`null` =
não observado).

### `PUT /v1/datasets/{dataset_id}`
Corpo: `{name*, task?, metadata?{}, created_at?, project_id?}` —
`name` não-vazio obrigatório. Resposta: `{dataset_id, project_id,
stored: true}`. Erros: `401`/`403` auth/escopo, `413`, `422`.

### `GET /v1/datasets?project_id`
Array puro `[{dataset_id, project_id, name, task, created_at}]`,
ordenado por `dataset_id`.

### `GET /v1/datasets/{dataset_id}?project_id`
`{dataset_id, project_id, name, task, created_at, metadata{}}` ou `404`.

### `PUT /v1/datasets/{dataset_id}/versions/{version}`
Corpo: `{cases*: [{case_id*, input?, expected?, allowed_tools?,
constraints?, rubric?}], description?, metadata?{}, created_at?,
project_id?}` — `cases` é lista (pode ser vazia) de objetos com
`case_id` não-vazio. Resposta: `{dataset_id, version, cases: n}`.
Erros: `404` dataset inexistente (crie-o antes), `409` versão já
existe — garantido por `INSERT … ON CONFLICT DO NOTHING` atômico,
sem janela check-then-act.

### `GET /v1/datasets/{dataset_id}/versions?project_id`
Array puro `[{dataset_id, project_id, version, description,
case_count, created_at}]` ordenado por `version`; `404` se o dataset
não existe.

### `GET /v1/datasets/{dataset_id}/versions/{version}?project_id`
`{dataset_id, project_id, version, description, created_at,
metadata{}, cases: [{case_id, input, expected, allowed_tools,
constraints, rubric}]}` (cases ordenados por `case_id`) ou `404`.

## Experiments (Cycle 13)

Specs de A/B persistidos — pinam cohorts + evaluators para que runs de
compare sejam reproduzíveis; cada execução é append-only em
`experiment_results` (o spec nunca reescreve o histórico). Ver
`docs/reference/experiments.md` para o modelo completo.

### `PUT /v1/experiments/{experiment_id}` — writer
`{name?, description?, dataset_id?, dataset_version?, cohorts: {a:
{trace_ids≤500, label?, config?}, b: {...}}, evaluators≤100, status?,
metadata?}` → `{experiment_id, stored: true}`. Upsert de spec:
`created_at`/`status`/`result_ref` preservados; `status` no corpo =
transição explícita (`open`/`completed`). `NaN`/`Infinity` no spec →
`422` (dado declarado inválido não é coagido silenciosamente).

### `GET /v1/experiments?project_id&limit&offset`
Envelope `{items, limit, offset}` ordenado por `experiment_id`.

### `GET /v1/experiments/{experiment_id}?project_id`
Spec armazenado + `runs` paginado (append-only, ordenado por
`run_id`) — ou `404`.

### `POST /v1/experiments/{experiment_id}/run` — writer
Executa o spec armazenado pelo MESMO caminho de `/v1/eval/compare`
(`evaluate_traces`/`compare_runs`/`apply_gate`) e anexa o relatório ao
histórico — `status` → `completed`, `result_ref` → `run:<id>`.
Cohorts resolvem apenas dentro do projeto do experiment (sem A/B
cross-tenant, diferente do compare ad-hoc). Corpo opcional
`{project_id?, gate?}`. Erros: `404` spec ausente, `422` spec
inválido.

## Inteligência — grafo e custo (Cycles 03/04/12)

### `GET /v1/graph/traces/{trace_id}?project_id`
Grafo de execução `ExecutionGraph`: nós = spans; arestas `parent`
(contenção observada), `sequence` (sucessão temporal entre irmãos por
`start_time`), `link` (`SpanLink`s — `dangling: true` quando o alvo não
está no grafo). `stats`: `span_count`, `edge_count`, `max_depth`,
`dangling_edges`, `missing_parents`, `cycle_members` (spans sentados
num ciclo de parentesco — parent chains corrompidos são evidência).
`404` se a trace não existe.

### `GET /v1/graph/traces/{trace_id}/critical-path`
`{path: [span_id…], total_ms, cycles: [{from, to}]}` — cadeia
raiz→folha que maximiza a soma de durações, avaliada sobre **todos** os
roots (traces parciais multi-root são cobertas). Ciclos de parentesco
são cortados deterministicamente — a aresta que fecha o ciclo aparece
em `cycles` (evidência), não derruba o endpoint. Cadeias arbitrariamente
profundas são iterativas (sem limite de recursão). Empates desempatam
por id lexicográfico.

### `GET /v1/graph/traces/{trace_id}/bottlenecks?top=5`
`{items: [{id, name, kind, self_ms, duration_ms}]}` — self-time =
duração do span menos a soma dos filhos diretos (clamp ≥ 0; filhos
podem se sobrepor). Ordenado por `self_ms` desc. `top` clampa a
0..100.

### `GET /v1/graph/sessions/{session_id}?project_id&limit=200`
Grafo de sessão: nós = traces ordenadas por `started_at`, arestas
`sequence` entre execuções consecutivas. `limit` clampa a 1..500;
`stats.truncated` indica que a sessão excede `limit` (evidência, não
erro).

### `GET /v1/graph/traces/{trace_id}/analysis?project_id`
Roll-up de centralidade/concentração sobre o `ExecutionGraph` (Cycle
12): `{nodes, edges, edge_kinds{kind:n}, max_out_degree{node, degree},
max_in_degree{node, degree}, out_degree_concentration,
failed_children_of_error_nodes[], repeated_node_signatures: [{kind,
name, count}]}`. `failed_children_of_error_nodes` = ids de nós cujo
pai errou (propagação observada); `repeated_node_signatures` lista
assinaturas `(kind, name)` com ≥3 nós. `404` se a trace não existe.

### `GET /v1/traces/{trace_id}/cost`
Relatório derivado (`provenance: "derived"`): `{span_costs[],
total_input_tokens, total_output_tokens, total_tokens,
total_estimated_usd, by_model{}, unpriced_models[],
usage_missing_on_llm_spans[], assumptions[], pricing_source, waste[]}`.
Estimativas vêm do `PricingRegistry` (tabela bundled ou `pricing_file`
JSON); `estimated_usd = null` quando o modelo não é precificado — nunca
inventado. `waste[]` lista findings evidenciados: `retry_amplification`
(≥3 invocações idênticas), `context_replay` (contexto reenviado ≥ maior
janela), `duplicate_delivery` (tokens gastos em spans `duplicate`/
`conflict`, com USD quando precificável), `unknown_tokens` (spans LLM
sem `token_usage`), `unpriced_model`.

### `GET /v1/analytics/pricing`
`{source, models: {model: {input_per_mtok, output_per_mtok,
cached_input_per_mtok}}}` — a tabela efetiva em uso.

## Investigação (Cycles 05/07/08/09)

### `GET /v1/traces/{trace_id}/failure?project_id`
Classificação determinística de falha: `{trace_id, failed, category,
confidence, evidence, detail}`. `failed` é verdade somente quando o
trabalho errou, nunca completou, foi cancelado, ou a topologia declarada
é impossível — flags de data-quality (`clock_skew`, `duplicate`, …) são
evidência, não veredicto. Categorias (§05.3): `cycle` (ciclo de
parentesco real — self-loop ou multi-nó, mesmo walk O(n) do graph
builder), `budget_exceeded` (429 nomeando quota/budget, ou flags
`*.budget.exceeded` — exaustão de conta, não throttle transitório),
`auth_error` (401 / `AuthenticationError` / credencial inválida),
`missing_permissions` (403 / `PermissionError` / forbidden),
`schema_mismatch` (schema/structured-output/parse — mais específico que
um 400 nu), `invalid_arguments` (400/422 / `ValidationError` family),
`retry_exhaustion` (counters `retry.*`/`attempt*` ≥ max registrado ou ≥
3, flags `retry.exhausted`, eventos `retry` ×3+, `RetryError`),
`rate_limit` (429 puro — um 429 recuperado não é causa), `timeout`
(`TimeoutError`/`deadline` em status, attrs ou exception events),
`agent_cancelled` (`CancelledError` / flags `cancelled` — span não-errored
com sinal forte também falha a trace; texto fraco tipo "order
cancelled" não conta), `tool_error`, `llm_error`, `guardrail` (erro em
span do kind), `incomplete` (spans nunca fechados — host provável
morto), `unknown`. Regras correm em ordem de especificidade sobre
evidência registrada (HTTP status em attrs, `exception.type` em attrs e
eventos, counters, flags, texto de status/eventos); `evidence` nomeia
`span_ids` + `signals` + `names` que dispararam. `unknown` com
`failed=false` é a resposta honesta para traces saudáveis. Hipóteses
de causa-raiz (§05.4) e sugestões de cenários de avaliação (§12.7) são
embutidas via `build_hypotheses`/`suggest_evaluators` (pacote
`forge_lens.investigate`), no bundle `/incident` e nos findings do
specialist `failure_investigator` — ver `docs/guides/failure-investigation.md`.

### `GET /v1/traces/{trace_id}/replay?project_id`
Replay **registrado** (não re-executa código do agente): timeline único
ordenado por `(timestamp, span_id, kind)` intercalando
`span_start`/`event`/`span_end` com `offset_ms` relativo ao primeiro
timestamp. `unclosed_spans` conta spans sem `end_time`.

### `GET /v1/traces/{trace_id}/incident?project_id`
Bundle de incidente versionado (`bundle_version`) compondo em um payload
JSON: `failure` + `hypotheses` (§05.4 — lista bounded por
`MAX_HYPOTHESES`) + `eval_suggestions` (§12.7 — candidatas) + `graph`
(stats, critical path, top-5 bottlenecks) + `cost` + `waste` + `replay`.
Um call, todo o evidence — feito para handoff/export.

O bundle passa por um **segundo boundary de sanitização no export**
(`redact_export`): dados já foram sanitizados na ingestão, mas findings
derivados podem ecoar corpos capturados verbatim sob `capture=full`.
Nesting é depth-capped (8) com marcador contado; valores sob chaves
identidade (`*_id`, `actor`) pulam máscara de e-mail — mascarar join
key quebraria correlação — mas credential patterns ainda se aplicam.
`export_sanitize: {redacted_keys, redacted_values, emails_masked,
dropped_content}` na resposta declara exatamente o que o scrub tocou.

### `GET /v1/traces/{trace_id}/security?project_id`
Observações de segurança MCP/tool-call (Cycle 08 — modo **observer**,
nunca bloqueia): `{trace_id, observations: [{kind, severity, span_id,
detail}], counts: {kind: n}, mode: "observer"}`. `kind` ∈
`risky_tool` (severity `review` — nome da tool casa a watchlist de
verbos: exec/eval/shell/rm/sudo/send_email/payment/…),
`secret_shaped_value` (severity `high` — valor com formato de segredo
sob um nome de atributo inócuo; sobreviveu ao redact por chave),
`sensitive_path` (`review` — `../`, `/etc/`, `~/.ssh`, `.env` em
argumentos). Ordenado: `high` primeiro, depois `kind`+`span_id`.

### `GET /v1/traces/{trace_id}/stream?project_id`
Replay SSE (`text/event-stream`) da trace armazenada — um evento por
span para UI progressiva: `event: meta` `{trace_id, spans}` → um
`event: span` por span ordenado por `(start_time, span_id)` →
`event: done`. `404` antes do stream abrir se a trace não existe.

### `GET /v1/traces/{trace_id}/loops?project_id&repeat_threshold=3`
Findings heurísticos de loop/sem-progresso (Cycle 07):
`{trace_id, findings: [{kind, classification: "heuristic", span_ids,
count, detail}], classification, note}`. `repeat_threshold` clampa a
1..50. Kinds: `repeated_identical_tool` (mesma tool + args idênticos
consecutivos ≥ threshold — assinatura sha256 de nome+args),
`retry_loop` (mesma tool ≥ threshold, todas com erro),
`repeated_agent_span` (span agent re-entrado ≥ threshold),
`no_progress_signal` (repetição co-ocorrendo com erros — emitido só
quando há spans com erro). Repetição é evidência, não veredicto —
polling/batch legítimo tem a mesma cara. `404` se a trace não existe.

## Avaliação (Cycle 06)

Avaliadores determinísticos sobre evidência armazenada — sem LLM, sem
relógio (`evaluated_at` só é emitido quando o caller injeta `now`;
idênticos inputs produzem relatórios byte-idênticos). Score `null` com
`details.reason` é a resposta honesta para dados insuficientes.

### `GET /v1/traces/{trace_id}/eval?project_id`
Bateria default (`error_free`, `tool_success`) → `{trace_id,
results[]}`.

### `POST /v1/eval/run`
`{trace_ids: [...≤500], evaluators: [{name, params?}]≤100, dataset?,
project_id?}` → `EvalReport`: `{dataset, trace_ids[], results:
{trace_id: [EvaluationResult]}, summary: {evaluator/metric: {n, nulls,
sum, mean_score, pass_rate}}}`. `trace_id` desconhecido → `404`
(trace-a-trace via `get_trace`). Erros: `400` JSON inválido, `413`,
`422` shape/`trace_ids` fora de 1..500/specs fora de 1..100.

### `POST /v1/eval/compare`
`{a: {trace_ids, dataset?, project_id?}, b: {...}, evaluators, gate?}`
→ `{a: {dataset, trace_ids, n}, b: {...}, deltas: [{evaluator, metric,
n_a, n_b, mean_a, mean_b, delta, pass_rate_a, pass_rate_b, test,
ci_a, ci_b, significance, p_value}], multiple_comparisons?}`.
`test` identifica o teste aplicado: `two_proportion_z` (métricas
binárias 0/1, n≥10 por lado) ou `mann_whitney_u` (métricas contínuas —
ex.: `token_f1` — sobre as amostras de score por resultado, aproximação
normal com correção de continuidade e ties; testa deslocamento de
distribuição, não média/mediana), `None` para avaliadores não
registrados. `ci_a`/`ci_b`: intervalos de Wilson 95% das pass rates
binárias (sempre dentro de [0,1], emitidos mesmo abaixo do piso como
descrição, não como teste). `significance`: `significant`/
`not_significant`, `insufficient_data` (n<10 por lado — recusa honesta),
`not_tested` (semântica do avaliador não verificada), `not_computable`
(variância pooled zero). Quando mais de um teste é computado, o campo
`multiple_comparisons` declara `"k tests at alpha=0.05 unadjusted"` —
interprete em conjunto com cuidado. Um side pode carregar seu próprio
`project_id`, sobrescrevendo o do topo apenas naquele lado —
comparação cross-project A/B. `gate` opcional `{metrics: {key:
{min_pass_rate_b?, max_drop?}}, fail_on_significant_regression?}` é
avaliado sobre os deltas e volta como `gate: {gate_passed,
failures[]}` — métrica não avaliada é falha de configuração, não
silêncio. Erros: `400`, `404` (trace de um dos lados), `413`, `422`.

Avaliadores: `error_free` (binário), `tool_success` (binário por trace —
pass = todas as tools ok; `null` sem tool spans), `latency_budget`
(param `budget_ms`, relógio do envelope), `token_budget` (param
`max_total_tokens`), `has_span` (binário; params `name`/`kind`
opcionais). Specs malformados retornam `score=null` + `reason` — um run
nunca 500 por spec inválido. Limites: ≤500 `trace_ids`, ≤100 specs.

## Eficiência e regressão (Cycle 12)

`null` numa métrica = sinal ausente, nunca zero inventado.

### `GET /v1/traces/{trace_id}/efficiency?project_id`
Roll-up da trace: `{trace_id, outcome, tool_calls, llm_calls,
error_spans, retries_observed, input_tokens, output_tokens,
usage_coverage: "n/m llm spans", succeeded}`. `retries_observed` soma
`gen_ai.tool.call.retry_count`/`retry.count`. `404` se a trace não
existe.

### `GET /v1/traces/{trace_id}/recommendations?project_id`
Recomendações **candidatas** com a evidência que as disparou:
`{trace_id, recommendations: [{kind, classification: "candidate",
…}], note}`. Kinds: `redundant_tool_calls` (tool+args idênticos em
loop — cache/dedup), `unstable_tool` (mesma tool com ≥2 falhas),
`context_budget` (span LLM com ≥100k input tokens),
`tool_call_volume` (≥30 tool calls). Ganhos não são reivindicados —
exigem validação experimental (`/v1/eval/compare`).

### `POST /v1/intel/efficiency`
Roll-up de frota. Corpo: `{project_id?, trace_ids?: [1..500 ids]}` —
sem `trace_ids`, usa a janela das ≤500 traces mais recentes do projeto.
Resposta: `{traces, succeeded, success_rate, tokens_per_trace,
tokens_per_success, tool_calls_per_trace, tool_calls_per_success,
retry_overhead, error_rate, note}` + `missing_trace_ids[]` quando ids
pedidos não existem (não é `404` — ausência vira evidência).

### `POST /v1/intel/regress`
Regressão entre coortes: `{a: {trace_ids, project_id?}, b: {...},
project_id?}` → `{label: "cohort", base_traces, candidate_traces,
deltas: {metric: {base, candidate, delta, pct}}, classification:
"observed_difference", note}` + `missing_trace_ids: {a, b}` quando há.
Métricas: `success_rate`, `tokens_per_trace`, `tokens_per_success`,
`tool_calls_per_trace`, `tool_calls_per_success`, `retry_overhead`,
`error_rate`. Deltas são **diferenças observadas** — o veredicto
estatístico é de `/v1/eval/compare`.

### `GET /v1/traces/{trace_id}/correlate?project_id`
§05.5 — sinais de co-ocorrência temporal dentro da trace:
`{trace_id, correlations: [{kind, classification:
"temporal_association", signals, strength, evidence_span_ids, caveat,
detail}], signal_inventory, note}`. Kinds: `retries_preceded_error`,
`tool_failure_cluster`, `model_switch`, `context_growth_preceded_error`,
`context_growth_preceded_eval`, `errors_preceded_eval_score`. Toda
correlação carrega caveat **não-causal** — associação temporal, nunca
reivindicação de causa. `404` se a trace não existe.

### `POST /v1/intel/correlate`
§05.5 — tabelas de co-ocorrência da frota. Corpo:
`{project_id?, limit?}` (`limit` ≤ 500, default 200 — janela das traces
mais recentes). Resposta: `{project_id, window, outcomes, tables:
{tool_error, model_error, session_length_error, retry_eval},
classification: "descriptive_co_occurrence"}`. Taxas por coorte
falha/ok com `n` completo; janelas sem ≥5 traces por lado são
`insufficient_data` com os números ainda visíveis — nunca taxas sem
base.

## Análise assistida — specialists (Cycle 07)

Specialists são analisadores determinísticos sobre evidência — sem
LLM, sob budgets cooperativos (iteração não é preemptada no meio;
execução em worker thread).

### `GET /v1/analyze/specialists`
`{specialists: […]}` — os 11 nomes registrados:
`context_efficiency_analyst`, `cost_analyst`, `evaluation_analyst`,
`evidence_validator`, `failure_investigator`, `graph_analyst`,
`loop_detection_specialist`, `report_specialist`,
`security_observation_analyst`, `tool_execution_analyst`,
`trace_investigator`.

### `POST /v1/analyze/{specialist}/{trace_id}`
Corpo: `{budget?: {max_iterations=8, token_budget=200000,
timeout_s=30.0}, context?: {}, run_id?, project_id?}` — campos de
budget devem ser números positivos finitos. Resposta: `{run_id,
specialist, stop_reason, iterations, tokens_observed, cancelled,
findings[]}`. `stop_reason` ∈ `completed` | `budget_exceeded` |
`token_budget_exceeded` | `timeout` | `cancelled` |
`unknown_specialist` — specialist desconhecido retorna `200` com
`findings` listando os disponíveis, não `404`. `context.pricing` é
sempre injetado pelo servidor (valor de wire é descartado). O
`failure_investigator` embute nos findings, além do `FailureReport`,
`hypotheses` (§05.4 — limitadas por `context.max_hypotheses`, default 5;
`hypothesis_count` + `hypotheses_truncated` declaram o corte) e
`eval_suggestions` (§12.7 — candidatas, datasets nunca são modificados).
Erros: `400`, `404` trace inexistente, `413`, `422` budget/context/
run_id malformado.

## Alertas (Cycle 07)

Regras são predicados de threshold sobre métricas agregadas do store,
computadas no momento do request e escopadas por `project_id`. Regra:
`{rule_id, metric, op, threshold, severity?, message?}` com `op` ∈
`>`, `>=`, `<`, `<=`, `==`; regra sem `rule_id`/`threshold`
numérico/métrica conhecida é ignorada.

### Métricas de alerta

Dois escopos de resolução — o de cada métrica está declarado na
tabela. Contadores vêm de `store.stats(project_id)` (corpus inteiro do
projeto — a janela que sempre tiveram). Métricas derivadas (duração,
custo, tokens, loops, erro por trace) precisam de dados por-trace que
o roll-up não carrega: resolvem sobre a **janela limitada das ≤200
traces mais recentes** por `started_at` — mesma convenção de
`/v1/ops/summary`, nunca um scan de frota (`loop_traces` roda
`detect_loops` em no máximo essas 200 — bound explícito). O endpoint
só resolve as métricas que as regras da chamada pedem: uma avaliação
puramente de contadores não escaneia traces.

| Métrica | Unidade | Escopo | O que conta |
|---|---|---|---|
| `span_count` | spans | corpus | spans persistidos |
| `trace_count` | traces | corpus | traces persistidas |
| `error_count` | spans | corpus | spans `status=error` |
| `error_rate` | fração 0–1 | corpus | `error_count` / `span_count` |
| `avg_duration_ms` | ms | janela | média de `ended_at − started_at` das traces encerradas; trace ainda rodando não tem duração observável — excluída, não zerada; 0.0 se nenhuma encerrou |
| `max_duration_ms` | ms | janela | idem, máximo |
| `p95_duration_ms` | ms | janela | percentil 95 por nearest-rank sobre a janela — o store não calcula percentis, é um rank de janela e não quantil de frota |
| `error_traces` | traces | janela | traces `status=error` — nível de trace, distinto de `error_count` (spans) |
| `error_trace_rate` | fração 0–1 | janela | `error_traces` / traces da janela |
| `token_total` | tokens | janela | soma de `forge_lens.token_usage` observada (`total_tokens` com fallback input+output, idêntico a `trace_cost`) |
| `cost_total_usd` | USD estimado | janela | soma de `trace_cost` por trace; uso de modelo ausente do `PricingRegistry` é **excluído** — ver `unpriced_model_count` |
| `cost_per_trace_usd` | USD estimado | janela | `cost_total_usd` / traces da janela |
| `unpriced_model_count` | modelos | janela | modelos distintos com uso observado mas sem preço no registry — o sinal de gasto cego que acompanha as métricas de custo |
| `loop_traces` | traces | janela | traces com ≥1 finding de `detect_loops` (heurístico — repetição pode ser polling legítimo) |

`eval_pass_rate` deliberadamente **não existe**: resultados de
`/v1/eval/run` são efêmeros (nunca persistidos) e reports gravados em
`experiment_results` são deltas por experiment/cohort/evaluator — um
escalar por projeto exigiria um seletor (experiment? cohort? métrica?)
que o schema da regra não carrega.

### `POST /v1/alerts/evaluate`
`{rules?: [≤100 objetos], use_saved?: bool, project_id?}` → `{fired:
[{rule_id, metric, op, threshold, observed, severity, message}],
evaluated, project_id}`. `use_saved: true` mescla as regras
persistidas em `<data_dir>/alert-rules.json`; o total combinado acima
de 100 → `422`.

### `POST /v1/alerts/rules` — admin, unscoped
`{rule: {rule_id*, …}}` → persiste (insert/replace por `rule_id`,
escrita atômica tmp+rename) → `{rules: […], path}`. Cap de 100 regras
salvas → `422` (delete antes). `403` para admin com escopo — regras de
alerta são globais.

### `DELETE /v1/alerts/rules/{rule_id}` — admin, unscoped
`404` se a regra não está no arquivo → senão `{rules: […]}` restantes.
`403` para admin com escopo.

## Ops

### `GET /v1/ops/summary?project_id`
Resumo operacional: `{project_id, stats{traces, spans, error_spans,
db_size_bytes, schema_version}, recent_traces, recent_failures:
[trace_id…≤50], note}` — janela limitada às 200 traces mais recentes
(`note` o declara), não um total de frota; `recent_failures` é a lista
de ids das que falharam, ordenada por `trace_id` DESC.

### `GET /v1/capabilities`
Matriz honesta de capacidades por host: `{hosts: {host:
{native_tracing, mechanism, adapter, coverage, limits}}, note}` —
`"unsupported"` é resposta válida: só consta o caminho verificado.

### `GET /v1/health`
`{status:"ok", version, storage:"sqlite", stats:{traces, spans,
error_spans, db_size_bytes, schema_version}}`. `stats` é omitido para
chave com escopo multi-projeto (sem fan-in); chave de um projeto só
recebe stats daquele projeto.

### `GET /v1/metrics`
Snapshot de self-observability: `{counters, gauges, histograms}` —
counters (`lens.api.requests`, `lens.ingest.spans`,
`lens.ingest.traces`, `lens.ingest.duplicates`,
`lens.ingest.rejected`, `lens.ingest.errors`, `lens.sanitize.*`,
`lens.api.errors`), gauges e
histogramas de latência (`{count, min_ms, max_ms, avg_ms}` —
`lens.ingest.latency_ms`, `lens.query.latency_ms`).

## Admin (Cycle 08) — role `admin`

### `POST /v1/admin/keys`
Emite uma chave gerenciada. Corpo: `{name*, role: reader|writer|admin,
projects?: [str]}` → `{key_id, secret, role, projects, warning}`. O
`secret` (`flk_sk_…`) é exibido **uma única vez** — só o hash fica no
store. Admin com escopo só emite chaves dentro do próprio escopo
(nunca unscoped, nunca projeto alheio) → `403`. Erros: `422` name
vazio/role inválido/`projects` não-lista.

### `GET /v1/admin/keys`
Array puro `[{key_id, name, role, projects, created_at,
revoked_at}]` — sem material de segredo. Admin com escopo vê apenas
chaves inteiramente contidas no seu escopo.

### `DELETE /v1/admin/keys/{key_id}`
Revoga (soft-delete: `revoked_at` preenchido) → `{key_id, revoked:
true}`. `404` inexistente ou já revogada; admin com escopo recebe
`403` se a chave-alvo sai do seu escopo (unscoped ou projeto alheio).

### `GET /v1/admin/audit?action&actor&limit=100&offset`
Trilha de auditoria — array puro `[{event_id, ts_ns, ts, actor,
action, resource, outcome, detail{}}]`, ordenado `ts` DESC.
`limit` fora de 0..500 → `422` (única rota que rejeita em vez de
clampar). Apenas admin **unscoped** → `403` para scoped (eventos não
carregam atribuição por projeto confiável).

### `POST /v1/admin/prune`
Retenção — deleta traces iniciadas antes de um cutoff (e seus spans;
granularidade de trace, nunca parcial). Corpo: `{before: ISO |
older_than_days: 0..106000}` — os dois são mutuamente exclusivos e um
é obrigatório. `project_id` delimita a um tenant (omitido →
`cfg.project_id`); `all_projects: true` é opt-in explícito de prune
global, exigindo admin unscoped. Resposta: `{deleted: {traces: n},
before_ns}`. Erros: `400`, `403` escopo, `413`, `422`.
