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
