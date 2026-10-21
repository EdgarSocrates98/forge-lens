# Versionamento & compatibilidade

- **SemVer estrito**: `0.x.y` — minor adiciona superfície, patch corrige.
  `1.0.0` só com decisão explícita registrada em ADR (ver AGENTS.md).
- **Contratos**: `contracts/v1` é congelado p/ mudanças incompatíveis.
  Campos novos são aditivos; remoção/rename exige `v2` + janela de
  depreciação de uma minor.
- **Migrações SQLite**: schema é versionado via migrations em
  `storage/sqlite.py`; `user_version` governa upgrades — downgrade não
  é suportado (exportar antes).
- **Compat matrix** (declarada, testada em CI): Python 3.11–3.14 ×
  SQLite ≥3.40; TS SDK em Node 20/22; Studio testado em CI via vitest/jsdom; matrix de browsers reais (Playwright) é follow-up documentado — não reivindicado.
- **Depreciação**: endpoint/símbolo marcado `deprecated:` em docs por
  uma minor antes da remoção; nunca removido silenciosamente.
