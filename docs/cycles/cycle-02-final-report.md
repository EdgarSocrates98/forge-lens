# Cycle 02 — Relatório Final: Forge Lens Studio & Trace Explorer

**Status**: `NOT_STARTED` — nenhum código de UI foi produzido neste ciclo.
**Data**: 2026-10-21

## 1. Estado do escopo

| Componente do spec (`prompt_evo_inicial.md` §Cycle 02) | Estado |
|---|---|
| Direção visual / design tokens (dark+light) | NOT_STARTED |
| Arquitetura Angular (standalone, strict TS, signals) | NOT_STARTED |
| Trace Explorer (lista, filtros, busca, sort) | NOT_STARTED |
| Waterfall view | NOT_STARTED |
| Graph view / session explorer | NOT_STARTED |
| Dashboards de custo/token | NOT_STARTED |

Não existe diretório `studio/`, `ui/` ou workspace Angular no repositório.
Nenhuma dependência frontend foi adicionada ao `package.json`/workspace —
apenas o SDK TypeScript (`packages/forge-lens-sdk-ts`) existe.

## 2. Por que ficou para trás

O roadmap foi executado na ordem backend-first: os ciclos 03–06 produziram
exatamente as APIs que o Studio consumiria (ver §3). Implementar a UI antes
do backend teria exigido mocks que quebrariam o princípio do projeto de
"evidência real ou ausência declarada" — uma UI contra dados fabricados não
é evidência de produto.

## 3. Backend disponível para o ciclo 02 (verificável)

Estas superfícies estão implementadas e testadas — são o contrato de dados
do futuro Studio:

| Necessidade da UI | API existente | Ciclo |
|---|---|---|
| Trace list + filtros | `GET /v1/traces` (project, session, status, limit/offset) | 01 |
| Trace detail | `GET /v1/traces/{id}` | 01 |
| Waterfall | `GET /v1/traces/{id}/replay` (timeline ordenado) | 05 |
| Graph view | `GET /v1/graph/traces/{id}`, `/sessions/{id}` | 03 |
| Critical path / bottlenecks | `GET /v1/graph/traces/{id}/critical-path`, `/bottlenecks` | 03 |
| Cost dashboards | `GET /v1/traces/{id}/cost`, `/v1/analytics/pricing` | 04 |
| Failure view | `GET /v1/traces/{id}/failure`, `/incident` | 05 |
| Eval panels | `GET /v1/traces/{id}/eval`, `POST /v1/eval/run`, `/compare` | 06 |

## 4. Dependências abertas antes de iniciar

- Definir transporte de auth para a UI (hoje a API exige `X-API-Key` quando
  `FORGE_LENS_API_KEY` está setada; `check_auth` em `api/app.py`).
- Paginação de `list_traces` é limit/offset — suficiente para v1 da UI;
  cursor pagination é decisão do ciclo 02, não bloqueio.
- CORS: `serve` é loopback-first; a UI servida em outra origem precisa de
  config explícita — decisão pendente no spec do ciclo 02.

## 5. Critérios de entrada quando o ciclo for retomado

1. Scaffold Angular standalone + strict TS em `studio/` (fora de `src/`).
2. Typed client gerado/manual contra os endpoints da tabela §3.
3. Trace list funcional primeiro; waterfall em seguida (usa replay).
4. Nenhum dado de demonstração fabricado — fixtures vêm de
   `examples/quickstart.py` ou de ingest real.
