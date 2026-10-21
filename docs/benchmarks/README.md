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
