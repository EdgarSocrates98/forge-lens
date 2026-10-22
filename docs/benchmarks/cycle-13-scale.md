# Benchmarks — ciclo 13.1 (homologação de escala)

> Execução real nesta máquina, contra a camada `tests/bench` estendida
> com volumetrias de 1k / 10k / 100k spans. Compara os números
> publicados (ciclo 01 e ciclo 09) com o que foi medido agora, com as
> mesmas métricas e nomes. **Não são SLAs.**

## Ambiente

| Item | Valor |
|---|---|
| CPU | AMD Ryzen 7 5700U with Radeon Graphics — 8 cores / 16 threads |
| RAM | 30 GiB (`MemTotal` = 31 467 MB) |
| Disco | NVMe SSD CL4-8D512 (`rotational=0`) |
| OS | Zorin OS 18.1 — `Linux-7.0.0-38-generic-x86_64-with-glibc2.39` |
| Python | 3.14.7 (`.venv`) |
| SQLite | 3.53.1 — WAL, `synchronous=NORMAL`, `busy_timeout=5000` |
| Carga | **loadavg ~10–33 durante todas as medições** (máquina compartilhada; ver "Limites") |

## Metodologia

- Corpus: `tests/bench/gen.py` — `make_bench_trace(i)`, função pura do
  índice. 4 spans/trace (`agent.run` → `llm` + 2 tools), ~10 % de erro,
  25 sessões, 8 tools, duração 900–1 500 ms. Idêntico em qualquer
  máquina; só o tempo varia.
- Escalas: **250 / 2 500 / 25 000 traces = 1k / 10k / 100k spans**.
- Seed = medida: ingest via `IngestPipeline.ingest` real
  (`capture=full`) em lotes de 1 000 traces (4 000 spans/request, sob
  os caps de 10k traces / 20k spans); cada lote é cronometrado → o
  registro carrega p50/p95/max por lote além do total.
- Latência por request: 100 ingestões de **1 trace** contra a store já
  populada → p50/p95/p99 reais (não amortizados por lote).
- Leituras: **mediana de 3 repetições** por superfície; cada repetição
  vai no JSON (`reps_ms`). Correção por fechamento de forma (ex.:
  `status=error` → n/10, `session-07` → n/25), não por timing.
- Eval: fan-out `get_trace` de 200 traces + registry completo (**17
  avaliadores** — cresceu de 12 no commit `56a6b68`).
- WAL: 3 conexões leitoras distintas sobre o mesmo arquivo (WAL real)
  enquanto o writer comita 80 ingestões single-trace.
- 3 execuções completas de `pytest tests/bench -m benchmark
  --bench-report=…` — run1 (loadavg≈19), run2 (≈31), run3 (≈26).
- Comparação ciclo-09: `scripts/benchmark.py --scale 1000 10000 100000`
  (mesmo corpus de 10 spans/trace e mesmo `upsert_trace` cru).
- **1M spans: não executado.** Melhor throughput medido = 472 spans/s →
  ≥ 35 min só de seed, fora do orçamento de ~10 min. Qualquer número de
  1M seria extrapolação — e extrapolação não é medida (regra do ciclo
  09 mantida).

## A — Comparação direta ciclo-09 (`scripts/benchmark.py`, mesmos nomes)

| Escala | Métrica | Publicado (c09) | Medido agora | Δ | Veredito |
|---|---|---|---|---|---|
| 1k spans (100 traces) | ingest | 0.555 s | 0.694 s | +25 % | dentro-de-variância? |
| | spans/s | 1 801 | 1 440 | −20 % | dentro-de-variância? |
| | list(500) | 8.45 ms | 8.64 ms | +2 % | dentro da variância |
| | get_trace | 4.6 ms | 6.31 ms | +37 % | dentro-de-variância? |
| | DB | 340 KB | 348 KB | +2 % | idêntico |
| 10k spans (1k traces) | ingest | 5.93 s | 8.26 s | +39 % | regressão aparente |
| | spans/s | 1 687 | 1 211 | −28 % | regressão aparente |
| | list(500) | 35.4 ms | 38.3 ms | +8 % | dentro da variância |
| | get_trace | 3.6 ms | 5.06 ms | +41 % | regressão aparente |
| | DB | 3.8 MB | 3.67 MB | −3 % | idêntico |
| 100k spans (10k traces) | ingest | 60.3 s | 96.7 s | **+60 %** | **regressão aparente** |
| | spans/s | 1 657 | 1 034 | −38 % | regressão aparente |
| | list(500) | 49.1 ms | 81.0 ms | +65 % | regressão aparente |
| | get_trace | 12.6 ms | 19.8 ms | +57 % | regressão aparente |
| | DB | 38 MB | 36.6 MB | −4 % | idêntico |

**Leitura honesta dos deltas:** execução única, sob loadavg≈18, contra
uma publicação cujo estado de carga é desconhecido — provavelmente mais
ocioso. A degradação cresce com a escala (+25 %→+60 % no ingest), o que
é compatível tanto com *mais trabalho por span no caminho de escrita*
quanto com *contenção acumulando ao longo de uma escrita mais longa*.
Um nível abaixo, as causas candidatas são:

- **Carga da máquina (dominante, medida):** loadavg 18 no instante do
  run — outras cargas de trabalho disputando CPU/IO num 5700U mobile.
  O spread de 2.2× entre runs da suíte (abaixo) mostra que a variância
  desta máquina, hoje, engole deltas de 20–60 %.
- **Caminho de escrita mais caro desde o ciclo 09 (plausível, não
  medido isoladamente):** `upsert_trace` ganhou digest por span,
  refresh de integrity flags e merges adicionais (rounds de review
  `e1815b0`+). Separar código de carga exige rerodada em máquina
  ociosa — **pendente honesto**, não concluível hoje.

## B — Ingest por escala (pipeline completo, `capture=full`)

| Escala | seed total (3 runs) | spans/s | lote p50 / p95 | single p50 | single p95 | single p99 |
|---|---|---|---|---|---|---|
| 1k spans (250 traces) | 2.12 / 5.24 / 2.63 s | 472 / 191 / 380 | 1.96 / 4.90 / 2.43 s | 6.9 ms | 9.8 ms | 11.0 ms |
| 10k spans (2 500) | 22.6 / 38.4 / 28.5 s | 442 / 260 / 351 | 7.9–12.0 / 8.4–17.9 s | 9.9–11.1 ms | 11.1–15.7 ms | 38.5–43.8 ms |
| 100k spans (25 000) | **237.8 / 314.4 / 531.5 s** | 421 / 318 / 188 | 8.4–19.3 / 12.1–25.8 s | 6.9–12.6 ms | 10.5–27.0 ms | 41.9–63.7 ms |

- **p50 por request é estável ~7–13 ms em qualquer tamanho de DB** —
  o custo por ingest é praticamente independente do corpus armazenado.
  Consistente com o `lens.ingest.latency_ms` ≈ 9.6 ms do ciclo 01
  (que incluía HTTP; aqui é só o engine — mesma ordem).
- p99 ~40–60 ms em todas as escalas: cauda de checkpoint WAL/fsync e
  contenção — visível só porque medimos por request.
- Spread de seed 100k entre runs = **2.2×** (238 s→531 s) com o mesmo
  código e mesmo corpus — a variância desta caixa hoje é o principal
  limitador da precisão destes números.
- Pipeline completo ≈ 0.2–0.5k spans/s aqui vs `upsert_trace` cru
  ≈ 1.0–1.4k spans/s — sanitize + flag_integrity + ORPHAN lookup por
  span custam ~3× o write puro. Preço conhecido da evidência.

## C — Leituras por escala (mediana de 3 runs; cada run já é mediana de 3 reps)

| Superfície | 1k | 10k | 100k | Crescimento |
|---|---|---|---|---|
| `list_traces` página 500 | 28.4 ms | 52.2 ms | 37.2 ms | ~plano |
| `status=error` | 2.9 | 26.5 | 324.6 | linear (retorna n/10 linhas) |
| `session_id` | 1.7 | 17.7 | 191.5 | linear (retorna n/25) |
| `q` (nome de span) | 9.0 | 100.7 | 1 386 | linear — LIKE `%..%` + EXISTS correlacionado |
| `tool` | 19.6 | 189.8 | 1 958 | linear — `json_each` por span |
| janela de duração | 11.6 | 106.9 | 1 546 | linear — `(ended-started)` sem índice de expressão |
| `since`+`until` | 7.4 | 8.0 | 8.6 | **plano** — `idx_traces_started` |
| `list_sessions` | 2.8 | 13.0 | 444.1 | linear — GROUP BY em n traces |
| `list_tools` | 7.0 | 70.2 | 1 049 | linear — `json_extract` + GROUP BY em n×2 spans |
| `get_trace` ×50 | 89.5 | 82.3 | 105.7 | ~plano (~1.6–2.1 ms/leitura) |

Nenhuma dessas linearidades é regressão: `q`/`tool`/janela/roll-ups são
scans sem índice aplicável por construção (wildcard de borda ou
expressão computada). Os predicados indexados (`started_ns`, `status`,
`session_id` + ordenação) permanecem planos — o plano de query está
correto onde há índice.

## D — Grafo e eval por escala

| Superfície | 1k | 10k | 100k |
|---|---|---|---|
| `build_session_graph` (n/25 traces) | 0.9 ms (10) | 4.8 ms (100) | 91.4 ms (1 000) |
| `build_trace_graph` árvore 10 000 spans | — | — | 1 779–2 774 ms (med 2 438) |
| `eval.fetch` (200 `get_trace`) | 394 ms | 209 ms | 594 ms |
| `eval.battery` (17 avaliadores × 200) | 418 ms | 422 ms | 712 ms |

- `eval.battery` é ~estável entre escalas (o custo é por trace, não por
  DB) — 396–1 028 ms de spread entre runs, ~2–5 ms por avaliação.
- O `eval.fetch` reflete o custo de leitura no tamanho do DB, não o dos
  avaliadores — por isso são registros separados.
- Grafo de sessão escala ~linear no nº de traces da sessão
  (0.9→91 ms para 10→1 000 nós); árvore de 10k spans ~2.4 s
  (vs 2k spans ~0.21–0.45 s — superlinear por alocação, esperado).

## E — WAL / concorrência (a nota, agora medida)

| Medida | 1k | 10k | 100k |
|---|---|---|---|
| leitura ociosa p50 (list 50) | 4.1 ms | 4.2 ms | 6.1 ms |
| leitura durante escrita p50 / p95 | 55.1 / 92.8 | 62.2 / 92.1 | 27.7 / 63.9 |
| escrita durante leituras p50 / p95 | 27.9 / 47.0 | 23.6 / 38.6 | 25.8 / 39.4 |

- 3 conexões leitoras × ~80–300 queries/escala, durante 80 commits
  single-trace: **zero erros, zero `SQLITE_BUSY` escapando** —
  `busy_timeout=5000` absorveu toda a contenção. Página lida
  permaneceu consistente (50 linhas do corpus estável).
- Leituras ficam **~5–14× mais lentas** durante churn de commit
  (checkpoint/fsync compartilhando a NVMe); escritas ~2–5× mais lentas
  sob leitores ativos. Não há bloqueio — há degradação. É o preço
  documentado do WAL local-first (ADR-0002), agora com número anexado.

## F — Camada smoke original vs publicado (mesmos nomes, `docs/reference/benchmarks.md`)

| Surface | Publicado (~ms) | Agora (3 runs, mediana) | Veredito |
|---|---|---|---|
| `corpus.seed` (1k traces, full) | ~6 300 | 9 226–18 600 (med 10 856) | regressão aparente (carga) |
| `ingest.pipeline` (500, metadata) | ~3 200 | 4 673–8 707 (med 6 507) | regressão aparente (carga) |
| `list_traces[page]` | ~49 | 73–189 (med 106) | dentro-de-variância? |
| `[status=error]` | ~10 | 12–15 | dentro da variância |
| `[session_id]` | ~6 | 5.5–8.4 | dentro da variância |
| `[q=span-name]` | ~34 | 41–51 | dentro da variância |
| `[q=trace-id]` | ~22 | 30–38 | dentro-da-variância? |
| `[tool]` | ~37 | 65–81 | regressão aparente leve |
| `[duration-window]` | ~42 | 42–61 | dentro da variância |
| `[since+until]` | ~8 | 7.8–11.3 | dentro da variância |
| `[last-page]` | ~13 | 13–36 | dentro da variância |
| `list_sessions` | ~7 | 6.9–9.8 | dentro da variância |
| `list_tools` | ~23 | 23–35 | dentro da variância |
| `get_trace[x50]` | ~53 | 63–98 | dentro-da-variância? |
| `build_trace_graph` 2k | ~233 | 210–453 | dentro da variância |
| `build_session_graph` 40 | ~3 | 2.9–3.5 | dentro da variância |
| `eval.fetch_traces` | ~200–510 | 229–522 | dentro da variância |
| `eval.battery` | ~180–470 (**12** evals) | 407–586 (**17** evals) | consistente — +41 % de avaliadores |

O aparente crescimento do `eval.battery` **não é regressão**: o
registry passou de 12 para 17 avaliadores (`56a6b68`) e a doc publicada
ficou defasada. Custo por avaliação ≈ 2–3 ms nas duas épocas.

## Limites conhecidos

- **Contaminação de carga.** loadavg 10–33 durante as medições (caixa
  compartilhada): números absolutos são pessimistas; os deltas
  publicado→agora carregam esse confundidor. Tratar "regressão
  aparente" como *sinal para rerodar ocioso*, não veredito.
- **Run 2 perdeu 4 registros de eval** (`NameError` transitório em
  `evaluators.py` — a worktree é compartilhada e o arquivo foi editado
  por outro trabalho durante a execução; rerodado limpo no run 3).
  Evidência de concorrência de trabalho, não de bug do engine.
- **1M spans não medido** — ver Metodologia.
- Escalas da suíte usam 4 spans/trace; o script do ciclo 09 usa
  10 spans/trace — `db_bytes` entre as duas tabelas **não** é
  comparável diretamente (o dobro de bytes por trace no corpus 4-span
  reflete atributos mais ricos, não overhead).
- Sem warm-up, sem mínimo-de-N nas seeds (o seed é o evento medido).
  Os `reps_ms` de cada leitura estão no `--bench-report` JSON.
- Os números valem para ESTA máquina e ESTA carga; o sinal útil
  portável é a *forma* do escalonamento (quais superfícies são planas
  vs lineares), não os absolutos.

## Como reproduzir

```bash
.venv/bin/python -m pytest tests/bench -q -m benchmark \
    --bench-report=/tmp/bench.json          # suíte completa (smoke+escala)
.venv/bin/python -m pytest tests/bench -m "not benchmark"  # exclui a camada
.venv/bin/python scripts/benchmark.py --scale 1000 10000 100000  # comparável ao ciclo 09
```
