# Benchmarks

> Ciclo 01: baseline de sanidade medida na máquina de desenvolvimento —
> não são números de produção. Reprodutível com `forge-lens serve` + os
> comandos abaixo.

## Medidos (ciclo 01, workstation local)

| Operação | Resultado observado |
|---|---|
| Ingest (4 spans, SDK→HTTP→SQLite) | ~9.6 ms no pipeline (`lens.ingest.latency_ms`) |
| `traces list` (1 trace) | sub-ms → ms |
| `traces show` (4 spans) | sub-ms → ms |

## Metodologia alvo (ciclos seguintes)

- Cargas: 1k/10k/100k spans por trace e por lote; ingest concurrent.
- Medir: p50/p95 ingest latency, tamanho do DB vs. spans, query latency
  de `list`/`show`, memória do processo, custo de sanitização.
- Publicar `docs/benchmarks/cycle-NN.md` com ambiente, comando e resultado.

## Medidos (ciclo 09, `scripts/benchmark.py`, execução real)

Ambiente: Python 3.14.7, SQLite 3.53.1, Linux x86_64 (workstation local).
Traces sintéticos de 10 spans; ingest via `SQLiteTraceStore.upsert_trace`
completo (sanitização + dedup + grafo de evidência do pipeline real).

| Escala | Ingest | spans/s | list(500) | get_trace | DB |
|---|---|---|---|---|---|
| 1k spans (100 traces) | 0.555 s | 1 801 | 8.45 ms | 4.6 ms | 340 KB |
| 10k spans (1k traces) | 5.93 s | 1 687 | 35.4 ms | 3.6 ms | 3.8 MB |
| 100k spans (10k traces) | 60.3 s | 1 657 | 49.1 ms | 12.6 ms | 38 MB |

Observações honestas:

- Throughput sustentado ~1.7k spans/s nesta máquina — **medido, não
  projetado**. 1M spans ainda não foi executado; a extrapolação linear
  sugerida (~10 min) NÃO é um número medido e não deve ser citada como tal.
- `list(500)` permanece <50 ms até 100k spans (índices em started_ns).
- Gargalo real: ingest é por-trace transacional (`INSERT OR REPLACE`
  por span). Caminhos de melhoria documentados: batch executemany,
  PostgreSQL/ClickHouse para escala multi-tenant (ADR pendente de
  necessidade real — o prompt exige justificativa, não especulação).

## Camada smoke contínua (ciclo 13, `tests/bench`)

Complemento deste script: `tests/bench/` roda a cada `pytest` e mede
pipeline completo + superfícies de leitura/grafo/eval sobre um corpus
determinístico de 1k traces. Ver `docs/reference/benchmarks.md` —
como rodar, superfícies medidas e caveats.

## Medidos (ciclo 13.1, homologação de escala — execução real)

A camada `tests/bench` foi estendida com volumetrias de **1k / 10k /
100k spans** (250 / 2 500 / 25 000 traces): seed via `IngestPipeline`
real em lotes cronometrados, latência single-trace com p50/p95/p99,
todas as leituras com mediana de 3 reps, grafo/eval por escala e a
nota de concorrência WAL tornado executável (3 conexões leitoras
durante 80 commits de escrita). 3 execuções completas; relatório
completo, deltas vs. ciclos 01/09 e limites em
[`cycle-13-scale.md`](cycle-13-scale.md).

Resumo honesto (máquina sob loadavg 10–33 — absolutos pessimistas):

| Métrica | Resultado |
|---|---|
| ingest por request (p50) | ~7–13 ms, estável em qualquer tamanho de DB |
| throughput pipeline (`capture=full`) | ~0.2–0.5k spans/s sob carga |
| `list_traces` indexados (`since`, página) | plano até 100k spans (~8–56 ms) |
| `q` / `tool` / janela de duração | lineares — 0.1–2 s em 100k spans (sem índice por construção) |
| WAL: leitura durante escrita | ~5–14× mais lenta, zero erros/`BUSY` |
| 1M spans | **não medido** — >10 min no throughput atual (fora do orçamento) |
