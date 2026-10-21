# Cycle 03 — Relatório Final: Inteligência de Grafo Temporal

**Status**: `PARTIAL` — backend implementado; Studio/UI permanece Cycle 02
**Data**: 2026-10-21

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| Modelos de grafo (`GraphNode`/`GraphEdge`/`ExecutionGraph`, `EdgeKind`) | `src/forge_lens/graph/models.py` | VERIFIED |
| Builder de grafo de trace (arestas `parent`/`sequence`/`link`) | `src/forge_lens/graph/builder.py` | VERIFIED |
| Builder de grafo de sessão (traces ordenadas por `started_at`) | `src/forge_lens/graph/builder.py` | VERIFIED |
| Algoritmos: `critical_path`, `self_time_ranking` | `src/forge_lens/graph/algorithms.py` | VERIFIED |
| Endpoints `/v1/graph/*` (graph, critical-path, bottlenecks, sessions) | `src/forge_lens/api/app.py` | VERIFIED |
| CLI `forge-lens graph <trace_id>` | `src/forge_lens/cli/main.py` | VERIFIED |
| Testes unitários + API | `tests/unit/test_graph_intel.py`, `tests/integration/test_api.py` | VERIFIED |

## 2. Semântica das arestas

- **`parent`** — contenção observada: `parent_span_id` presente no
  conjunto de spans. Parent ausente **não** gera aresta nem nó
  inventado — a evidência fica no flag `missing_parent` do nó e em
  `stats.missing_parents`.
- **`sequence`** — sucessão temporal entre irmãos (mesmo parent,
  incluindo nível raiz), ordenados por `start_time`.
- **`link`** — `SpanLink` observado; alvo fora do grafo vira
  `dangling: true` (link cross-trace é evidência, não erro).

Determinismo: nós em ordem `(start_time, span_id)`; arestas em
`(kind, source, target)` — mesmo input, mesmo grafo.

## 3. Algoritmos

- **`critical_path`** — cadeia raiz→folha maximizando a soma de
  durações: o trabalho serializado que domina o wall-clock do trace.
  Empates por id lexicográfico. `O(n)`.
- **`self_time_ranking`** — `duration − Σ filhos diretos`, clamp ≥ 0
  (filhos podem se sobrepor; piso zero em vez de trabalho negativo
  inventado). Identifica onde o trace *gasta* tempo, não orquestra.
- **`max_depth`** — maior cadeia parent→folha (BFS, imune a ciclos de
  parent corrompidos via conjunto `seen`).

## 4. O que NÃO está neste ciclo (honesto)

- Grafo multi-trace por causalidade/entidade (agent/tool cross-trace):
  apenas `link` edges observadas são projetadas — não há inferência de
  arestas causais não declaradas pelo produtor.
- Layout/visualização — é Cycle 02 (Studio); o JSON deste módulo é a
  fonte que a UI consumirá.
- Detecção de loops temporais entre execuções — Cycle 07 (a heurística
  `retry_amplification` em Cycle 04 cobre o caso intra-trace).

## 5. Evidência

- `test_trace_graph_*`, `test_critical_path_*`, `test_self_time_*`,
  `test_session_graph_*` — unitários sobre spans sintéticos.
- `test_trace_graph_endpoint`, `test_critical_path_and_bottlenecks`,
  `test_session_graph_endpoint` — integração sobre `TestClient`.
- `forge-lens graph <id>` imprime stats + critical path + top self-time.
