# Cycle 01 — Relatório Final: Ingestão Real e Trace Recuperável

**Status**: `VERIFIED` · **Decisão**: `APPROVED`
**HEAD**: primeiro commit deste ciclo
**Data**: 2026-10-21

## 1. Escopo implementado (tudo verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| Contratos v1 + JSON Schema | `src/forge_lens/contracts/`, `schemas/v1/` | VERIFIED |
| Core (ids W3C, tempo, hash, errors, métricas, config) | `src/forge_lens/core/` | VERIFIED |
| Normalização OTLP/HTTP JSON + mapa OpenInference | `src/forge_lens/otlp/` | VERIFIED |
| Sanitização (redaction, capture levels, truncation) | `src/forge_lens/sanitize/` | VERIFIED |
| Store SQLite WAL + protocolo `TraceStore` | `src/forge_lens/storage/` | VERIFIED |
| Pipeline (limites, rate-limit, integrity flags, dedupe) | `src/forge_lens/ingest/` | VERIFIED |
| API FastAPI (ingest nativo+OTLP, query, health, metrics) | `src/forge_lens/api/` | VERIFIED |
| SDK Python (tracer, decorators, contexto, usage, exporter) | `src/forge_lens/sdk/` | VERIFIED |
| CLI (init/serve/traces list+show/ingest/doctor) | `src/forge_lens/cli/` | VERIFIED |
| SDK TypeScript zero-dep + node:test | `packages/forge-lens-sdk-ts/` | VERIFIED |
| CI (3 OS × py, 2 Node × TS, pip-audit, schema drift check) | `.github/workflows/ci.yml` | VERIFIED |
| Docs PT-BR (research, arch, ADRs, guides, refs, install, security) | `docs/` | VERIFIED |

## 2. Quality gate do ciclo: "ingestão real + trace recuperável"

**VERIFIED** — evidência real executada (não simulada):

```
SDK LensTracer (agent→llm+tools, 1 erro) → HTTPExporter → POST /v1/traces
→ sanitize (prompt body dropado, capture=metadata) → SQLite WAL
→ GET /v1/traces/{id} devolve árvore completa + token_usage
→ forge-lens traces show renderiza árvore com !erro e ~tokens
→ /v1/metrics reporta lens.ingest.* reais
```

Trace observado no smoke test: `47da2021625f82ef5414f7a72db60798`
(4 spans, 1 tool error, `input.value`→`dropped`, tokens preservados).

## 3. Testes e cobertura

- **61 testes Python verdes**: unit (17), contract+golden (13), integration
  (11), e2e (3), CLI (6) + hashing/config (5) — `pytest -q` em 6.5s.
  *(Contagem do commit original — os addendos de review
  `docs/cycles/cycle-01-review-fixes.md` subiram para 123 py / 17 ts.)*
- **7 testes TS verdes**: `npm test` (tsc + `node:test`).
- **Cobertura total 90%** (term-missing no CI).
- Golden fixtures: `tests/fixtures/{otlp-request,golden-normalized-trace,
  native-ingest}.json` — normalização byte-estável.
- Lint: `ruff check` limpo. Tipos: `mypy src` strict, 37 arquivos, 0 erros.

## 4. Segurança verificada

- `pip-audit`: **0 vulnerabilidades** conhecidas.
- Sanitização provada em teste: `sk-*`, `access_token`, e-mail, PEM, JWT,
  Bearer → redigidos/mascarados/dropados conforme nível.
- Auth de ingestão: 401 sem/com chave errada, 200 com `X-Forge-Lens-Key`.
- Limites: 413 (payload/spans), 429 (rate-limit), 400 (JSON), 422 (OTLP).
- **Bug real encontrado e corrigido**: regex de chave sensível com "token"
  solto destruía `llm.token_count.*`/`forge_lens.token_usage` — agora
  token-types são enumerados (regressão coberta).
- **Bug real**: import do módulo API criava `.forge-lens/` — store agora é
  lazy; side-effect removido.
- **Bug real**: `traces show` duplicava nós filhos na árvore — corrigido.
- **Bug real**: `doctor` ignorava o `path` — corrigido.
- **Bug real**: envelope `project_id` não era aplicado nas traces (API+CLI) —
  corrigido em ambos.

## 5. APIs entregues

`POST /v1/traces`, `POST /v1/otlp/traces`, `GET /v1/traces`,
`GET /v1/traces/{id}`, `GET /v1/health`, `GET /v1/metrics` — ver
`docs/reference/api.md`.

## 6. Findings

| Sev | Item | Estado |
|---|---|---|
| P0 | nenhum aberto | — |
| P1 | OTLP protobuf ausente | DOCUMENTED_ONLY/backlog |
| P1 | Cobertura `otlp/normalize` 80% e `sdk/context` 72% | backlog de testes |
| P2 | `forge_lens.token_usage` serializa campos extra (`schema_version`, `provenance`) dentro de attributes | cosmetic, backlog |
| P2 | `serve` sem hot-reload/testado só via smoke manual | NOT_TESTED em CI (processo) |

## 7. Limitações

- E2E real validado manualmente via servidor local + SDK (documentado acima);
  testes automatizados cobrem o mesmo fluxo via TestClient (sem socket real).
- Métricas são in-process — exportar o Lens via OTLP é decisão futura
  (evita auto-instrumentação recursiva).
- Estado na data deste relatório (numeração canônica do roadmap):
  Studio/UI `DOCUMENTED_ONLY` (ciclo 02); graph engine `DOCUMENTED_ONLY`
  (ciclo 03 — implementado depois, ver `cycle-03-final-report.md`);
  replay simulado/re-execução `DOCUMENTED_ONLY` (ciclo 05 — observed
  replay implementado depois); evals: contrato pronto, runner
  `DOCUMENTED_ONLY` (ciclo 06 — implementado depois); MCP:
  `DOCUMENTED_ONLY` (ciclo 10). Status atual: `docs/architecture/overview.md`.

## 8. Backlog para o ciclo 02

- Query de spans (filtros por atributo/nome), paginação rica.
- Export de trace para arquivo (`forge-lens export`).
- Mais ramos de normalização OTLP (kvlist, bytes, arrays) com cobertura.
- Início do Trace Explorer web (prep para ciclo 03) e ingest contínuo.

## 9. Prontidão próximo ciclo

Base pronta: store+pipeline+contratos suportam queries ricas e o Explorer.
Nenhum P0 aberto. Decisão: **APPROVED**.
