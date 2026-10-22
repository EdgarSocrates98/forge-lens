# Guia — Agent Graph Explorer

> Estado: **backend implementado** (ciclo 03) e **UI implementada**
> (`apps/studio`, rota `/graph`) — coberta pela suíte E2E Playwright
> (`docs/studio-e2e.md`). O grafo também segue consumível via API/CLI.

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

## O que a UI faz hoje

A rota `/graph` renderiza o grafo em SVG interativo sobre essas mesmas
APIs:

- Modo `trace` ou `session`, com deep-link `?trace_id=…`/`?session_id=…`.
- Pan por arrastar, zoom por roda, busca com destaque de nós e toggle
  de **critical path** (arestas/nós do caminho realçados).
- Seleção de nó mostra adjacências `in`/`out`; arestas `sequence`,
  `link` e `stub` têm traços visuais distintos e `dangling_edges`
  renderizam como badge `dangling` — evidência declarada, não ausência
  silenciosa.
- Banners de truncamento: `stats.truncated` do session-graph
  (server-side) e o cap de nós desenhados (client-side) são exibidos.
- Tabela de **bottlenecks** (self-time) no modo trace.

Filtros por agente, ferramenta, modelo, sessão e janela temporal vivem
na página `/traces` (lista), que leva ao detalhe/grafo da trace
escolhida. O caminho causal de falha segue investigável via
`GET /v1/traces/{id}/failure` e o painel `failure` do detalhe.

## Convenção visual preservada

- `provenance` em tudo → nada no grafo apresenta hipótese como fato:
  arestas/nós `inferred` (quando existirem) serão visualmente distintos
  de `observed`.
- `dangling_edges` e `truncated` são evidência declarada e renderizam
  como tal — badge e banner, nunca silêncio.
