# Cycle 11 — Relatório Final: Release Engineering, Quality Freeze & Open Source

**Status**: `IMPLEMENTED` — SBOM CycloneDX, release workflow com OIDC +
gate de versão + ambiente `pypi` com aprovação manual, CHANGELOG,
versioning policy e jobs de CI completos; matriz de browsers reais
seguia como follow-up declarado.
**Data**: 2026-10-21
**Entrega**: commit `bd47625` (entrega consolidada c02/07–12). Reporte
original resumido em `cycle-06-12.md`; este arquivo é o relatório
individual exigido pela spec §10.

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| Gerador de SBOM CycloneDX a partir do ambiente instalado | `scripts/gen_sbom.py` | VERIFIED |
| Release workflow — OIDC trusted publishing, version gate (input = pyproject), publish em environment `pypi` com aprovação explícita | `.github/workflows/release.yml` | VERIFIED |
| CI — matrix py (ubuntu/macos/windows) + sdk-ts (node 20/22) + studio build/tests + schema-sync check + `uv build` + `pip-audit` | `.github/workflows/ci.yml` | VERIFIED |
| Dependabot — uv / npm / github-actions semanal | `.github/dependabot.yml` | VERIFIED |
| `CHANGELOG.md` (Keep a Changelog + SemVer) | `CHANGELOG.md` | VERIFIED |
| Versioning & compat policy — SemVer, contracts freeze, migration, deprecation, compat matrix declarada | `docs/versioning.md` | VERIFIED |
| Security disclosure privado | `SECURITY.md` | VERIFIED |

## 2. Semântica honesta

- **Publicação exige humano**: `publish` roda no environment `pypi`
  (required reviewers) e o job `verify` aborta se o input não bater com
  `pyproject.toml` — sem release acidental.
- **SBOM do ambiente real**: gerado do env instalado na pipeline de
  release, não de manifesto teórico.
- **Compat matrix declarada, não inflada**: `versioning.md` diz
  explicitamente que Studio é testado via vitest/jsdom e que browser
  matrix real (Playwright) é follow-up — linguagem "não reivindicado".
- **Schema-sync gate**: CI falha se `schemas/v1/` derivar dos contratos.

## 3. Exclusões honestas (não implementado — deferrals declarados)

- **Browser matrix real** (§11.4 — Chromium/Firefox/WebKit): deferral
  documentado; Studio hoje = unit tests jsdom + build em CI.
- **Real-world validation com traces autorizados** (§11.10): fase de
  adoção — exemplos e fixtures existem (`examples/`, `tests/golden/`),
  mas claims de produção exigem dados reais ainda não coletados.
- **Community assets do §12.9**: issue templates + public roadmap +
  adoption matrix eram gap de review — **fechados nesta wave**
  (`.github/ISSUE_TEMPLATE/`, `docs/roadmap.md`, `docs/adoption.md`).

## 4. Evidência de verificação

- `ci.yml` roda em push/PR: ruff, mypy strict, pytest, schema-sync,
  `uv build`, sdk-ts (node 20/22), studio.
- 288 testes py verdes no commit de entrega; suite hoje >500 itens
  coletados incluindo golden scenarios e bench smoke.
- `release.yml` nunca publicou sem gate — `environment: pypi` exige
  aprovação configurada no GitHub.
