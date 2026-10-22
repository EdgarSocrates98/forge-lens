# Graph Explorer — escala e limites medidos

> Estado: **implementado** (ciclo 13.1) em `apps/studio/src/app/graph.ts`.
> Fixtures determinísticas de escala: `apps/studio/src/app/graph-scale.fixture.ts`.
> Assertions de performance: `apps/studio/src/app/graph-scale.spec.ts`
> (os números abaixo saem do log `[perf]` dessa suite).

## Arquitetura de render

SVG hand-rolled, zero dependências — a decisão de manter SVG (não
canvas/WebGL) se sustenta porque o custo real é limitado por três
técnicas combinadas:

1. **Cap de nós desenhados (`MAX_NODES = 500`)** — o conjunto desenhado
   é um **prefixo BFS a partir da raiz** (`bfsOrder`), não um `slice`
   arbitrário: todo nó desenhado tem o pai desenhado (prefixo
   *parent-closed*), portanto o subgrafo visível é conexo e navegável.
   O banner honesto "showing N of M" permanece.
2. **Culling por viewport** (`vlnodes`/`vledges`) — só instancia no DOM
   o que intersecta o viewBox com margem de 40%. O teste de arestas é
   conservador por bounding-box: um segmento cujo bbox não toca a
   viewport nunca a cruza, então nada visível é cortado. Pan/zoom geram
   churn incremental via diff do `@for` — só entram/saem os elementos da
   borda.
3. **Layout em `computed` preguiçoso** (`layeredPositions`, Kahn O(V+E))
   — cacheado pelo grafo de signals; pan, zoom, seleção e busca **não
   recomputam posições**. Só `load()` e mudanças no conjunto `revealed`
   invalidam. Em modo zoneless as escritas de signal já coalescem CD —
   não há rAF/debounce extra porque cada passe checado é O(visível).

Detalhes operacionais:

- **Camera clamp** (`clampVb`) — pan/zoom/centralização não escapam do
  conteúdo: slack máximo de metade do viewport além das bordas.
- **Stubs honestos, deduplicados** — arestas para endpoints fora do
  conjunto desenhado viram um stub por nó por direção (`out`/`in`), em
  vez de uma linha por aresta escondida (num grafo denso seriam milhares
  de linhas coincidentes). `stats.edge_count` segue com a contagem real.
- **Arestas in/out da seleção** — destacadas (âmbar = entrantes,
  verde = salientes) e listadas no painel com clique-para-atravessar.

## Números medidos

Máquina: AMD Ryzen 7 5700U, 32 GB, Linux x86_64, Node v22.23.2,
suite vitest+jsdom do Studio (`npx ng test`). Valores da execução do
ciclo (variação ~±15% entre runs):

| Operação | Tamanho | Medido | Teto assertado |
|---|---|---|---|
| `extremeGraph` generate | 10 000 nós | ~402 ms | 3 000 ms |
| `bfsOrder` | dense 3 000 (~10k arestas) | ~78 ms | 500 ms |
| `bfsOrder` | extreme 10 000 | ~110 ms | 500 ms |
| `layeredPositions` | tree 500 (cap inteiro) | ~18 ms | 150 ms |
| `layeredPositions` | 500 desenhados de dense 3 000 | ~42 ms | 300 ms |
| Render componente no cap (jsdom, teste e2e do spec) | 500 nós | ~3,4 s* | — |

\* jsdom é 10–30× mais lento que Chrome em criação de DOM; no browser
real a primeira pintura de 500 nós (≈2 500 elementos) é centenas de ms.
O número importa como *regressão*, não como absoluto.

## Limites práticos

| Perfil | Interatividade |
|---|---|
| ≤ 500 nós | confortável — tudo desenhado, culling só ajuda no zoom |
| 500–3 000 nós | confortável com cap — prefixo BFS navegável + busca/expand revelam o resto |
| 3 000–10 000 nós | utilizável — ordenação BFS ~100 ms uma vez por carga; payload e painel de stats intactos |
| > 10 000 nós | degradado — API/store passam a dominar; o cap continua honesto mas a UX é "buscar, não explorar" |

## Técnicas aplicadas vs. rejeitadas

- **Rejeitado: canvas/WebGL** — o gargalo após cap+culling é o paint do
  que está em view, que já é pequeno; reescrever o renderer não paga o
  diff. **Rejeitado: debounce/rAF em pan/zoom** — CD zoneless já
  coalesce por tick; adicionar latência de frame só pioraria a
  resposta. **Rejeitado: layout incremental por coluna** — Kahn inteiro
  é <45 ms no pior caso desenhável; complexidade injustificada.

## Riscos residuais

- `bfsOrder`/`matches` varrem o payload inteiro (O(V+E) / O(V·|q|)) —
  ~100 ms a 10k nós; acima disso a busca ficaria sensível no input.
- `fit()` num cap de 500 desenha tudo — o culling só reduz DOM depois
  que o usuário dá zoom; a primeira pintura de grafos muito largos é
  pixel-densa (honesto, mas ilegível sem zoom).
- Stubs deduplicados escondem *quantas* arestas saem de vista — a
  contagem real só existe em `stats` e no painel do nó selecionado.
- Grafos com um nível gigante (milhares de irmãos) geram viewBox muito
  alto — o clamp segura a navegação, mas o overview é estreito.
