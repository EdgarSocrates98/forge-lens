# Cycle 09 — Relatório Final: Escalabilidade, Performance & Confiabilidade

**Status**: `IMPLEMENTED` — índices, replay SSE e benchmark **real
medido** até 100k spans; scalable mode (Postgres/ClickHouse) avaliado e
descopeado por ausência de necessidade medida; 1M spans não executado e
não reivindicado.
**Data**: 2026-10-21
**Entrega**: commit `bd47625` (entrega consolidada c02/07–12). Reporte
original resumido em `cycle-06-12.md`; este arquivo é o relatório
individual exigido pela spec §10.

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| Índices em traces/spans (started_ns, status, session, project) | `src/forge_lens/storage/sqlite.py` | VERIFIED |
| Replay SSE `GET /v1/traces/{id}/stream` | `src/forge_lens/api/app.py` | VERIFIED |
| `scripts/benchmark.py` — ingest real via pipeline completo | `scripts/benchmark.py` | VERIFIED |
| Números medidos publicados | `docs/benchmarks/README.md` | VERIFIED |

## 2. Resultados medidos (não projetados)

Ambiente: Python 3.14.7, SQLite 3.53.1, Linux x86_64, workstation local.
Traces sintéticos de 10 spans via `SQLiteTraceStore.upsert_trace`
completo (sanitize + dedupe + grafo de evidência do pipeline real).

| Escala | Ingest | spans/s | list(500) | get_trace | DB |
|---|---|---|---|---|---|
| 1k spans | 0.555 s | 1 801 | 8.45 ms | 4.6 ms | 340 KB |
| 10k spans | 5.93 s | 1 687 | 35.4 ms | 3.6 ms | 3.8 MB |
| 100k spans | 60.3 s | 1 657 | 49.1 ms | 12.6 ms | 38 MB |

- Throughput sustentado ~1.7k spans/s **nesta máquina** — medido.
- `list(500)` <50 ms até 100k (índices em `started_ns`).
- Gargalo honesto documentado: ingest transacional por trace.
- Pós-review (cycle 13): camada contínua `tests/bench` (marker
  `benchmark`) mede pipeline + leitura/grafo/eval a cada `pytest` —
  doc em `docs/reference/benchmarks.md`.

## 3. Exclusões honestas (não implementado — deferrals declarados)

- **1M spans** (§09.9): não executado — extrapolação linear (~10 min)
  NÃO é número medido e não pode ser citada. Requer ambiente de
  benchmark apropriado (a própria spec condiciona).
- **Scalable mode Postgres/ClickHouse** (§09.3): avaliado e deferido —
  sem necessidade medida em escala local-first; novo backend exige ADR
  (regra do projeto). Caminho registrado em
  `docs/architecture/scalability.md` (`TraceStore` protocol, instância
  por operador + export).
- **Filas/broker** (§09.3): spec proíbe introduzir sem necessidade
  demonstrada — não demonstrada.
- **Graph scalability avançada** (§09.7 — clustering, level-of-detail,
  viewport persistence): bounded expansion existe; LOD/clustering é
  deferral (ver `docs/roadmap.md`).

## 4. Evidência de verificação

- `python scripts/benchmark.py` reproduz os números acima; metodologia e
  caveats em `docs/benchmarks/README.md`.
- 288 testes py verdes no commit de entrega; CI multi-OS.
- SSE coberto na suíte de API; tenancy 403 também no endpoint de stream.
