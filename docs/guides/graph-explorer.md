# Guia — Agent Graph Explorer

> Estado: **backend implementado** (ciclo 03); **UI não implementada**
> (ciclo 02 Studio, `NOT_STARTED`). Hoje o grafo é consumido via API/CLI.

## O que já existe hoje

O motor de grafo está implementado — ver `docs/architecture/graph.md`:

- `GET /v1/graph/traces/{trace_id}` — nós (spans tipados por `SpanKind`) +
  arestas `parent`/`sequence`/`link`.
- `GET /v1/graph/traces/{trace_id}/critical-path` — caminho dominante de
  duração, cycle-safe.
- `GET /v1/graph/traces/{trace_id}/bottlenecks?top=N` — ranking por
  self-time.
- `GET /v1/graph/sessions/{session_id}?limit=N` — grafo de sessão (um nó
  por trace) com `stats.truncated` quando a janela corta.

## O que a UI (ciclo 02) trará

- Navegação interativa agente→ferramenta→modelo→delegação sobre essas
  mesmas APIs.
- Caminho causal de falha destacado visualmente (hoje investigável via
  `GET /v1/traces/{id}/failure`, ciclo 05).
- Filtros por agente, ferramenta, modelo, sessão, janela temporal.

## Convenção visual que a UI deve preservar

- `provenance` em tudo → arestas/nós `inferred` (quando existirem) serão
  visualmente distintos de `observed` — o grafo nunca apresenta hipótese
  como fato.
- `dangling_edges` e `truncated` são evidência declarada, devem renderizar
  como tal — não como ausência silenciosa.
