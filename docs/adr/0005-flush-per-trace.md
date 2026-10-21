# ADR-0005 — Flush por trace (não por span) no SDK

**Status**: ACCEPTED (ciclo 01)

## Contexto

Exportadores por span geram leituras de meia-árvore (filhos antes do pai,
runs incompletos) e N requests pequenas; por trace, o consumidor vê a
execução atomicamente na maioria dos casos.

## Decisão

`LensTracer` acumula spans por `trace_id` em memória; `auto_flush` dispara
quando o **span raiz** fecha. `flush(trace_id?)` manual suporta traces
longas — o store faz merge determinístico entre flushes parciais.

## Consequências

- Leitura típica = árvore completa num único ingest.
- Traces muito longas podem `flush()` no meio — merge já implementado e
  testado (started=min/ended=max, status precedence).
- Limite: um agente "infinito" sem flush cresce em memória do SDK —
  documentado; mitigável com flush periódico ou caps futuros.

## Alternativas rejeitadas

- **Span-a-span**: leituras parciais constantes + mais I/O; dedupe seguiria
  funcionando, mas UX do Explorer seria pior.
- **Batch por tempo**: latência variável e árvore ainda fragmentada.
