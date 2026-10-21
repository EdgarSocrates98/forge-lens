# Benchmarks — camada smoke (`tests/bench`)

> A camada `tests/bench` mede **tempo de parede real** sobre as
> superfícies do engine — store, pipeline, grafo, eval — chamadas
> diretamente (sem uvicorn, sem sockets). São sinais de sanidade
> executados a cada `pytest`, não SLAs nem claims de capacidade.
>
> Complementar a `scripts/benchmark.py` (ciclo 09): o script mede
> `upsert_trace` cru em escala (1k–100k spans); a camada smoke mede o
> pipeline completo e as superfícies de leitura com seletividade
> conhecida, com assertivas de *correção* e teto generoso apenas em
> ingest.

## Como rodar

```bash
uv run pytest tests/bench            # só a camada bench
uv run pytest -m benchmark           # idem, via marker registrado
uv run pytest -m "not benchmark"     # suíte sem a camada bench
uv run pytest tests/bench --bench-report=/tmp/bench.json
```

Cada execução imprime a tabela de timings no fim da sessão
(`pytest_terminal_summary`). `--bench-report=PATH` adicionalmente grava
JSON com ambiente (`python`, `platform`, `sqlite`) + registros
`{surface, seconds, **detail}` — cole esse arquivo ao documentar
números, nunca digite de memória.

## O que é medido

Corpus sintético determinístico (`tests/bench/gen.py`): cada trace é
função pura do índice — timestamps fixos a partir de
`T0 = 2026-01-01Z`, ids hex por contador, 4 spans (agent → llm + 2
tools), `session-{i % 25}`, ~10 % de erro, duração 900–1 500 ms, 8
tools × 250 calls por 1 000 traces. Sem relógio de parede, sem
aleatoriedade: a *carga* é idêntica em qualquer máquina; só o *tempo*
varia.

| Registro (surface) | Superfície | Escala |
|---|---|---|
| `corpus.seed` | `IngestPipeline.ingest` (fixture, `capture=full`) | 1 000 traces / 4 000 spans |
| `ingest.pipeline` | `IngestPipeline.ingest` em DB novo (`capture=metadata`) | 500 traces / 2 000 spans |
| `store.list_traces[*]` | `list_traces` sem filtro e com `status`, `session_id`, `q`, `tool`, `min/max_duration_ns`, `since/until_ns`, paginação | store com 1k traces |
| `store.list_sessions` | roll-up `GROUP BY session_id` | 25 sessões |
| `store.list_tools` | roll-up `json_extract(tool.name)` | 4 000 spans → 8 tools |
| `store.get_trace[x50]` | leitura ponto-a-ponto | 50 traces |
| `graph.build_trace_graph` | projeção nó/aresta de 1 trace | 2 000 spans (árvore binária) |
| `graph.build_session_graph` | grafo temporal de sessão | 40 traces |
| `eval.fetch_traces` | fan-out `get_trace` (espelha `POST /v1/eval/run`) | 200 traces |
| `eval.battery` | `evaluate_traces` — registry completo | 12 avaliadores × 200 traces |

Assertivas: conteos exatos de seletividade (ex.: `status=error` → 100,
`session-07` → 40, `q="code_review"` → 250) e integridade do output.
O único teto de tempo é `ingest.pipeline < 30 s` — ~10× folga sobre o
medido, tripwire para regressão catastrófica em CI carregada.

## Medido nesta máquina (dev workstation, execução real)

Ambiente: Python 3.14.7, SQLite 3.53.1, Linux x86_64, Ryzen 7 5700U,
16 cores, 30 GB RAM. Execução única via `pytest` — variância de ±10–20 %
entre rodadas é normal.

| Surface | Tempo medido |
|---|---|
| `corpus.seed` (1k traces, capture=full) | ~6.3 s |
| `ingest.pipeline` (500 traces, metadata) | ~3.2 s (~1 550 spans/s) |
| `list_traces` página 500 | ~49 ms |
| `list_traces` `status=error` | ~10 ms |
| `list_traces` `session_id` | ~6 ms |
| `list_traces` `q` (nome de span) | ~34 ms |
| `list_traces` `q` (trace_id exato) | ~22 ms |
| `list_traces` `tool` | ~37 ms |
| `list_traces` janela de duração | ~42 ms |
| `list_traces` `since`+`until` | ~8 ms |
| `list_traces` última página (offset 900) | ~13 ms |
| `list_sessions` | ~7 ms |
| `list_tools` | ~23 ms |
| `get_trace` ×50 | ~53 ms (~1 ms/leitura) |
| `build_trace_graph` (2 000 spans) | ~233 ms |
| `build_session_graph` (40 traces) | ~3 ms |
| `eval.fetch_traces` (200 reads) | ~200–510 ms |
| `eval.battery` (12 × 200) | ~180–470 ms (<2 ms por avaliação) |

## Caveats honestos

- **Hardware-dependente.** Números absolutos só valem para a máquina e
  a execução em que foram medidos — trate deltas entre superfícies
  (ordens de magnitude) como o sinal útil, não o valor bruto.
- **Execução única, sem percentis.** A camada não faz warm-up, mínimo
  de N rodadas nem p50/p95; para estatística de verdade, rode
  `scripts/benchmark.py` ou um runner dedicado.
- **Smoke ≠ SLA.** O teto de 30 s em ingest existe para pegar
  regressão catastrófica (ex.: O(n²) novo), não para prometer
  throughput. Não converta esses números em claims de marketing ou
  capacidade sem rodada dedicada e ambiente documentado.
- **`corpus.seed` usa `capture=full`** para que `output.value`
  sobreviva à sanitização e a bateria de eval produza scores reais
  sobre evidência armazenada; `ingest.pipeline` usa o default
  `metadata` — o caminho de produção.
- **Sem efeito de rede.** HTTP/uvicorn/serialização de fio ficam fora
  por decisão — a camada é offline e determinística em tudo exceto o
  relógio que existe para medir.
