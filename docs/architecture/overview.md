# Arquitetura — Visão Geral

> Status: **implementado** no ciclo 01 para o núcleo descrito como "atual";
> itens marcados *roadmap* estão documentados mas não construídos.

## Princípios estruturais

1. **Local-first**: todo o pipeline roda em processo único, SQLite WAL,
   `127.0.0.1` — nenhuma dependência obrigatória de nuvem, container, fila
   ou serviço externo.
2. **Núcleo puro / bordas sujas**: `core`, `contracts`, `otlp`, `sanitize`,
   `storage`, `ingest` são determinísticos e sem rede; `api/` (FastAPI) e
   `sdk/exporter` são as únicas fronteiras de I/O de rede.
3. **Contratos versionados são a fronteira de compatibilidade**: tudo que
   entra ou sai passa por `contracts/v1` (`schema_version` explícito) ou por
   OTLP. JSON Schemas gerados vivem em `schemas/v1/`.
4. **Epistemic spine**: `Provenance` (`observed|imported|derived|inferred|
   unknown`) acompanha Trace/Span/Evidence/EvaluationResult/ToolInvocation/
   AgentExecution. Dados ausentes ficam `None`.
5. **Safe-by-default**: `capture=metadata` sanitiza segredos e descarta corpos
   de prompt/tool antes de persistir.

## Componentes (atual)

```
┌────────────┐   ┌──────────────┐   ┌───────────────┐
│ SDK Python │   │ SDK TS       │   │ OTLP clients  │
│ (tracer)   │   │ (zero-dep)   │   │ (OpenInference│
└─────┬──────┘   └──────┬───────┘   │  /OTel)       │
      │ TraceIngest     │           └───────┬───────┘
      ▼                 ▼                   │ ExportTraceServiceRequest
┌───────────────────────────────────────────▼───────────┐
│                  FastAPI boundary (api/)               │
│   POST /v1/traces    POST /v1/otlp/traces              │
└───────────────────────────┬────────────────────────────┘
                            ▼
┌─────────────────────────────────────────────┐
│              IngestPipeline                  │
│ validate → sanitize → flag integrity →       │
│ dedupe → persist        (limits+rate-limit)  │
└───────────────────────────┬──────────────────┘
                            ▼
┌─────────────────────────────────────────────┐
│        SQLiteTraceStore (WAL)                │
│  traces + spans tables, indexes, schema ver  │
└───────────────────────────┬──────────────────┘
                            ▼
┌─────────────────────────────────────────────┐
│ Query layer: /v1/traces, /v1/traces/{id},    │
│ /v1/health, /v1/metrics · CLI traces show    │
└─────────────────────────────────────────────┘
```

## Componentes (roadmap)

- **Trace/Graph Explorer UI** (Studio) — ciclo 03/04.
- **Graph engine temporal** — ciclo 04 (camada sobre o store).
- **Replay/simulação** — ciclo 06 (observed replay é a única leitura hoje).
- **Evaluations/experimentos** — ciclo 07.
- **Specialist agents / loop detection** — ciclos 05, 08.
- **MCP instrumentation** — ciclo 09, opcional e declarado com escopo real.
- **Export/integração Forge** — ciclo 10 (via artefatos, sem acoplamento).

## Fluxo de uma execução (verificado e2e no ciclo 01)

1. `LensTracer` acumula spans por `trace_id`; flushes atômicos por trace.
2. Envelope `TraceIngest` (schema v1) via `HTTPExporter` → `POST /v1/traces`.
3. Pipeline aplica limites → sanitiza → marca `integrity_flags` → deduplica
   por `(project_id, trace_id, span_id)` → persiste em transação única.
4. Leitura reconstrói `Trace` completa: spans ordenados, token usage, erros,
   flags de integridade — tudo com provenance explícito.

## Escolhas deliberadas

- **Flush por trace (não por span)** para leituras atômicas do caso comum;
  traces longos podem `flush()` parcial — o store faz merge determinístico.
- **OTLP protobuf rejeitado explicitamente** (só JSON) — não anunciamos codec
  que não entregamos; protobuf é backlog com prioridade honesta.
- **Auth de leitura aberta**: modelo local de usuário único; `api_key` só
  protege ingestão. Documentado no threat model.
