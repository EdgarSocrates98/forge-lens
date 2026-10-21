# Arquitetura — Grafo Agêntico

> Estado: **implementado** (ciclo 03) em `src/forge_lens/graph/` — projeção
> determinística de traces/sessões em nós+arestas, exposta via
> `/v1/graph/*`. Relatório: `docs/cycles/cycle-03-final-report.md`.

## Nós (hoje)

- `GraphNode` por `Span` — `id=span_id`, `kind` tipado por `SpanKind`
  (`agent|tool|llm|…`), `name`, `status`, `duration_ms`, token usage.
- Session graph (`build_session_graph`): um nó por `Trace`, com stats
  agregados e `truncated` explícito quando a janela corta.

## Arestas implementadas (`EdgeKind`)

| Aresta | Origem evidencial |
|---|---|
| `parent` | `parent_span_id` observado no span filho |
| `sequence` | sucessão temporal entre siblings ordenados por `start_time` |
| `link` | `SpanLink` explícito — pode apontar para nó fora do grafo (`dangling_edges` conta isso) |

## Algoritmos

- `critical_path` — caminho de maior duração acumulada; iterativo e
  cycle-safe (ciclos em `parent` não travam nem corrompem o resultado).
- `self_time_ranking` — bottlenecks por tempo próprio (duração − filhos),
  `duration_ms` negativo de clock-skew clampado a 0.
- `cycle_member_ids` — detecção O(n) de membros de ciclo, exposta em
  `ExecutionGraph.stats`.

## Endpoints

- `GET /v1/graph/traces/{trace_id}` — grafo completo da trace.
- `GET /v1/graph/traces/{trace_id}/critical-path`
- `GET /v1/graph/traces/{trace_id}/bottlenecks?top=N` (N ≤ 100)
- `GET /v1/graph/sessions/{session_id}?limit=N` (N ≤ 500)

## Não implementado (futuro)

- Arestas semânticas `calls_tool`/`invokes_model`/`delegates_to`/`retries`
  e `produces`/`consumes` (BlobRef) — requerem projeção além de
  parent/sequence/link; não são inferidas.
- Agregação de custo por sub-árvore no grafo (hoje custo por span via
  `/v1/traces/{id}/cost`, ciclo 04).
- Loop/stagnation detection — ciclo 07; ciclo em `parent` é detectado como
  anomalia estrutural, não como sinal de loop agêntico.

## O que o grafo **não** faz

- Não infere aresta por correlação temporal — ausência de `parent_span_id`
  ou `SpanLink` registrado fica sem aresta, não vira hipótese.
- Não apresenta `link` para nó ausente como falha — vira `dangling_edges`
  em `stats`, evidência declarada.
