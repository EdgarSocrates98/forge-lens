# Arquitetura — Modelo de Eventos e Entidades

## Contratos v1 (`src/forge_lens/contracts/v1/`)

Todos os modelos são Pydantic, `schema_version="1.0.0"`, `extra="allow"`
(campos desconhecidos preservados — forward-compat).

| Modelo | Papel | Campos-chave |
|---|---|---|
| `Trace` | execução completa/parcial | trace_id, project_id, session_id, root_span_id, spans[], provenance |
| `Span` | unidade de trabalho | ids, parent, kind, tempos, attributes, events, links, status, integrity_flags |
| `SpanEvent`/`SpanLink` | pontos e arestas | name+time+attrs / trace_id+span_id+attrs |
| `AgentExecution` | run de agente | agent_id, run_id, parent_run_id, model, tool_calls[], refs |
| `ToolInvocation` | chamada de ferramenta | tool_name, input/output refs, retry, status |
| `TokenUsage` | contadores | input/output/cached/reasoning/total, measurement_source |
| `CostObservation` | custo | estimated_cost **≠** billed_cost, pricing_source, assumptions[] |
| `Evidence` | artefato verificável | source, origin_type (Provenance), hash, classification, sanitization_status |
| `EvaluationResult` | métrica de avaliador | evaluator(+versão), dataset(+versão), score, confidence, refs |
| `BlobRef` | referência a payload | kind (inline/uri/sha256/artifact/dropped), size, sha256 |
| `TraceIngest` | envelope de ingestão | project_id, source, traces[] |
| `Provenance` | enum epistêmico | observed/imported/derived/inferred/unknown |

## Regras epistêmicas

1. `estimated_cost` e `billed_cost` nunca são mesclados — estimado é
   `derived` (registry de preços), faturado é `observed` (report do provider).
2. `measurement_source`: `provider` (reportado), `estimated` (tokenizador
   local), `unknown` — contador ausente fica `None`.
3. `parent_run_id` só é setado quando a delegação foi observada.
4. `Evidence.origin_type` registra como a evidência chegou — uma hipótese
   (`inferred`) nunca é promovida a `observed`.

## Payloads fora do contrato

Corpos grandes (prompts, args, outputs) vivem como `BlobRef` ou como
atributos sujeitos a capture-level — o contrato carrega *referência e
metadados*, não o conteúdo por padrão.
