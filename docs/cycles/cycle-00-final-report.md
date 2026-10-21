# Cycle 00 — Relatório Final: Fundação, Contratos e Arquitetura

**Status**: `VERIFIED` · **Decisão**: `APPROVED`
**HEAD**: primeiro commit deste ciclo (repo sem histórico anterior)
**Data**: 2026-10-21

## 1. Escopo executado

| Item do ciclo | Resultado | Evidência |
|---|---|---|
| Auditoria de mercado | Langfuse, Phoenix, LangSmith, Datadog LLM, AgentOps, Weave, Helicone, Grafana/Tempo analisados | `docs/research/market-landscape.md`, `docs/research/competitive-analysis.md` |
| Validação OTel/OpenInference | Convenções `openinference.*` + `gen_ai.*` mapeadas; W3C traceparent implementado | `src/forge_lens/otlp/openinference.py`, `core/ids.py`, testes |
| Arquitetura local-first independente | núcleo puro + bordas de I/O; SQLite WAL; sem deps externas obrigatórias | `docs/architecture/overview.md`, ADRs 0001–0006 |
| Contratos versionados | v1 completa: Trace/Span/AgentExecution/ToolInvocation/TokenUsage/CostObservation/Evidence/EvaluationResult/BlobRef/TraceIngest + Provenance | `src/forge_lens/contracts/v1/`, `schemas/v1/*.json` |
| Definição do MVP | ingest → persist → query de traces reais/sintéticas; explorer via CLI/API | este relatório + código do ciclo 01 |
| Estrutura do repositório | src-layout, tests/, packages/, schemas/, docs/, .github/ | árvore do repo |
| ADRs | 6 decisões registradas | `docs/adr/` |

## 2. Decisões arquiteturais (ADRs)

- **0001** Contratos = Pydantic 2 (`pydantic>=2.7`) com `schema_version` v1 + JSON Schema gerado commitado (CI verifica drift). ACCEPTED.
- **0002** SQLite WAL é o store default (único arquivo, transacional, zero infra). ACCEPTED.
- **0003** Sanitização pré-persistência com `capture=metadata|full`. ACCEPTED.
- **0004** `Provenance` obrigatório — espinha epistêmica. ACCEPTED.
- **0005** SDK flushes por trace (leitura atômica). ACCEPTED.
- **0006** OTLP JSON-only (protobuf rejeitado explicitamente). ACCEPTED.

## 3. Contratos entregues

`schemas/v1/`: trace, trace-ingest, span, agent-execution, tool-invocation,
token-usage, cost-observation, evidence, evaluation-result — todos com
`schema_version: "1.0.0"`, `extra="allow"`, provenance explícito.

## 4. Descobertas de mercado que moldaram o design

- OTel+OpenInference venceu o transporte → Lens ingere exatamente isso.
- Ninguém separa custo estimado de faturado → `CostObservation` separa.
- "Replay" do mercado é marketing → 3 modos nomeados (observed default).
- Lacuna local-first radical → SQLite único, nenhum serviço extra.

## 5. Quality gate do ciclo

**"Contratos e arquitetura aprovados"** — ✅ contratos implementados + schemas
gerados + ADRs registrados + testes de contrato verdes (61 testes no ciclo 01).

## 6. Findings

| Sev | Item | Ação |
|---|---|---|
| P0 | nenhum aberto | — |
| P1 | OTLP protobuf não suportado | declarado + backlog (ADR-0006) |
| P2 | UI/Studio inexistente ainda | planejado ciclo 03 |
| P2 | cobertura de normalize ~80% (ramos exóticos de AnyValue) | backlog de testes |

## 7. Limitações honestas

- Mercado analisado por conhecimento de domínio + docs públicos, não por
  benchmark hands-on de cada concorrente (`VERIFIED_WITH_FIXTURES` para
  afirmações comparativas).
- OpenInference semconv está em movimento — o mapa é tolerante, não exaustivo.

## 8. Prontidão para o ciclo 01

Implementação da fundação (feito neste mesmo PR por decisão de agrupar
Etapa 1): ingest OTLP+nativo, sanitize, store, API, SDK Py/TS, CLI, CI,
testes — ver `cycle-01-final-report.md`.
