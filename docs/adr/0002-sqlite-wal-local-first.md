# ADR-0002 — SQLite WAL como store local-first default

**Status**: ACCEPTED (ciclo 00)

## Contexto

Local-first exige: zero infra externa, durabilidade ACID, portabilidade
(arquivo único), leitura durante escrita. Candidatos: SQLite, DuckDB,
arquivos JSONL/Parquet, embedded K/V.

## Decisão

SQLite (stdlib) com `journal_mode=WAL`, `busy_timeout`, `synchronous=NORMAL`,
schema versionado em `lens_meta`, transação por `upsert_trace`, dedupe por
PK `(project_id, trace_id, span_id)`, atributos em JSON canônico.

## Consequências

- Um `lens.db` move a instalação inteira; apagar o arquivo = expurgo real.
- Escrita serializada (1 writer) — suficiente p/ single-user; mitigável por
  fila no pipeline se necessário.
- JSON em colunas sacrifica consulta estrutural profunda; colunas quentes
  (name/kind/status/tempos) cobrem as queries atuais.
- Backends alternativos ficam atrás do protocolo `TraceStore` — ClickHouse/
  Parquet podem entrar depois sem tocar o pipeline (e são *opcionais*,
  nunca requisito).

## Alternativas rejeitadas

- **DuckDB**: analytics forte, mas OLTP/writer e packaging pesam mais que
  o ganho no escopo local.
- **JSONL append-only**: simples mas sem transação/índice/dedupe — queries
  ruins e merge manual.
- **Postgres/ClickHouse default**: viola local-first (infra obrigatória).
