# Arquitetura — Grafo Agêntico

> Estado: **roadmap (ciclo 04)**. A fundação de dados já persiste tudo que o
> grafo precisa; o motor de consulta temporal e o Graph Explorer são
> documentados aqui, não implementados. Status: `DOCUMENTED_ONLY`.

## Nós

- `AgentExecution` (por `run_id`), `Span` (granular), `ToolInvocation`,
  `ModelCall` (span kind=llm), `Session`, `Project`.

## Arestas (todas com intervalo temporal quando observável)

| Aresta | Origem evidencial |
|---|---|
| `contains` | trace→span (PK), session→trace |
| `parent_of` | `parent_span_id` observado |
| `calls_tool` | span kind=tool dentro de agente / `ToolInvocation` |
| `invokes_model` | span kind=llm + `llm.model_name` |
| `delegates_to` | `parent_run_id` / link entre traces — só quando observado |
| `retries` | `retry_count`>0 / mesmo tool_call_id repetido |
| `produces`/`consumes` | refs de BlobRef entre spans (futuro) |

## Consultas alvo (ciclo 04)

- Sub-árvore de uma execução com ordem temporal exata.
- Caminho causal candidato de uma falha (ancestrais de spans `error`).
- Difusão de custo: agregar `TokenUsage`/`CostObservation` por sub-árvore.
- Delegações observadas entre agentes/runs.

## O que o grafo **não** faz

- Não infere aresta por correlação temporal — ausência de `parent_run_id`
  registrado vira `unknown`, não aresta inventada.
- Não classifica repetição legítima como loop — detecção de loop/no-progress
  é ciclo 05 e exige evidência explícita de estagnação.
