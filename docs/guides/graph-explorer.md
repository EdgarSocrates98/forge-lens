# Guia — Agent Graph Explorer

> Estado: **`DOCUMENTED_ONLY`** (ciclo 04). Os dados que alimentam o grafo
> já são persistidos hoje; a UI e o motor de consulta temporal não existem
> ainda.

## O que você poderá fazer

- Ver agente→ferramenta→modelo→delegação como grafo temporal navegável.
- Traçar o caminho causal candidato de uma falha (ancestais de spans `error`).
- Agregar custo de tokens por sub-árvore.
- Filtrar por agente, ferramenta, modelo, sessão, janela temporal.

## O que já existe hoje (base de dados)

- `parent_span_id` → arestas `parent_of`/`contains`.
- `SpanKind` (`agent|tool|llm|…`) → tipagem dos nós.
- `forge_lens.token_usage` → custo por nó.
- `SpanLink` + `parent_run_id` (quando observado) → arestas entre runs.
- `provenance` em tudo → arestas `inferred` serão visualmente distintas de
  `observed` — o grafo nunca apresenta hipótese como fato.
