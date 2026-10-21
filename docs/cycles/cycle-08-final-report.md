# Cycle 08 — Relatório Final: Segurança, Privacidade & Governança

**Status**: `IMPLEMENTED` — auth modes `none|key|keys` com API keys
hashed + roles + tenancy por projeto, audit trail sem segredos, retenção
prune/archive e passthrough TLS; encryption-at-rest descopeada com
racional (deployer scope).
**Data**: 2026-10-21
**Entrega**: commits `1be01c8` (auth/retenção/TLS) + `bd47625`
(consolidação). Reporte original resumido em `cycle-06-12.md`; este
arquivo é o relatório individual exigido pela spec §10.

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| Tabela `api_keys` — segredos hashed, role, escopo por projeto | `src/forge_lens/security/keys.py`, `storage/sqlite.py` | VERIFIED |
| Tabela `audit_events` — trilha admin sem segredos | `storage/sqlite.py` | VERIFIED |
| Auth modes `none|key|keys`; `reader|writer|admin` por rota | `src/forge_lens/api/app.py` | VERIFIED |
| Escopo central `_get_trace_or_404` — rota que esquece `_require_scope` ainda não vaza | `src/forge_lens/api/app.py` | VERIFIED |
| Endpoints `/v1/admin/{keys,audit,prune}` | `src/forge_lens/api/app.py` | VERIFIED |
| Retenção: `prune_traces`, `forge-lens prune`, `forge-lens archive` (export-before-delete), `POST /v1/admin/prune` | `src/forge_lens/cli/main.py`, `api/app.py` | VERIFIED |
| TLS no `serve` — `--ssl-certfile` / `--ssl-keyfile` | `src/forge_lens/cli/main.py` | VERIFIED |
| `api/clock.py` — único seam de wall-clock (audit/retenção apenas) | `src/forge_lens/api/clock.py` | VERIFIED |
| Testes | `tests/unit/test_security.py`, matriz de tenancy E2E | VERIFIED |

## 2. Semântica honesta / decisões registradas

- **E2E de tenancy**: toda rota de leitura (traces, graph, ops, alerts,
  intel, analyze, eval) testada com key `reader` de p1 contra dados de
  p2 → 403 em todas; stats globais (health/ops/alerts) também scopados.
- **Decisões deliberadas de superfície** (trade-off, não oversights):
  `/docs` + `/openapi.json` ficam sem auth em `keys` mode (disclosure é
  só de schema, tooling precisa); `/v1/health` exige `reader` em `keys`
  mode — probes carregam key. Documentado no runbook e no roadmap.
- **Audit sem segredos**: eventos registram ações (key.create,
  prune, archive, alert_rule.*) — nunca tokens/keys.
- **Wall-clock isolado**: `api/clock.py` é o único ponto de tempo civil;
  engine nunca lê relógio (AGENTS.md, exceção documentada 2).

## 3. Exclusões honestas (não implementado — deferrals declarados)

- **Encryption at rest** (§08.7): escopo do deployer (FDE de SO) —
  racional e gatilho de revisita em `docs/roadmap.md`.
- **SSO/OIDC multi-usuário**: fora do escopo local-first; `keys` mode
  cobre local/team; não existe ainda interface de auth pluggable
  (pré-requisito registrado no roadmap).
- **Secret management / key rotation** (§08.7): gestão de segredos é do
  ambiente do operador; rotação de API keys = delete + create (auditado).

## 4. Evidência de verificação

- 288 testes py verdes no commit de entrega consolidada; matriz de
  tenancy descrita acima roda na suíte (`tests/`).
- `ruff`/`mypy --strict` limpos; CI multi-OS.
- Threat model atualizado registra os resíduos aceitos
  (`docs/security/threat-model.md` — "Resíduos aceitos / roadmap").
