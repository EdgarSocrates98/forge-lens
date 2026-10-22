# Arquitetura — Visão Geral

> Status: núcleo de ingest/storage implementado no ciclo 01; backends de
> grafo (ciclo 03), custo/tokens (ciclo 04), investigação+replay (ciclo 05)
> e avaliação (ciclo 06) implementados — ver `docs/cycles/`. Itens marcados
> *roadmap* estão documentados mas não construídos.

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
│ Query/Intel layer (implementada)             │
│  /v1/traces*, /v1/graph/*, /v1/traces/{id}/  │
│  cost·failure·replay·incident·eval,          │
│  /v1/eval/run·compare, /v1/health, /v1/      │
│  metrics · CLI traces show                   │
└─────────────────────────────────────────────┘
```

## Componentes implementados além do núcleo

- **Graph engine** (ciclo 03) — `graph/` reconstrói nós/arestas por trace e
  sessão: critical path, self-time/bottlenecks, cycle-safe.
- **Token/cost intelligence** (ciclo 04) — `intel/` pricing registry,
  `TokenUsage`→`estimated_usd`, waste signals.
- **Investigação/replay** (ciclo 05) — `investigate/` failure
  classification, observed replay timeline, incident bundles.
- **Evaluation backend** (ciclo 06) — `eval/` evaluators determinísticos,
  `EvalReport`, A/B compare com z-test honesto.

## Componentes (roadmap)

- **Studio/Trace Explorer UI** (Angular) — implementado em `apps/studio`
  sobre as APIs acima (rotas `/traces`, `/traces/:id`, `/graph`, …; E2E
  Playwright em `docs/studio-e2e.md`).
- **Agentic ops / loop detection / alerts** — ciclo 07.
- **RBAC, retenção, auditoria, criptografia** — ciclo 08.
- **Postgres/ClickHouse path + benchmarks 100k–1M spans** — ciclo 09.
- **MCP instrumentation + host adapters + installer** — ciclo 10,
  opcional e declarado com escopo real.
- **Release engineering / quality freeze** — ciclo 11.
- **Specialist analysis agents / advanced intelligence** — ciclo 12.

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
