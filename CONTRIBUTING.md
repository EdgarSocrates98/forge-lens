# Contributing — Forge Lens

Obrigado pelo interesse! Forge Lens é open-source (MIT) e local-first.

## Setup

```bash
uv sync --extra server --all-groups
python -m pytest -q && python -m ruff check . && python -m mypy src
cd packages/forge-lens-sdk-ts && npm install && npm test
```

## Regras de ouro

1. **Contratos são versionados.** Mudou `contracts/v1`? Bumpe `schema_version`,
   rode `python -m forge_lens.contracts.jsonschema`, commite `schemas/v1/` e
   os fixtures golden. Nunca quebre consumidores de payloads antigos.
2. **Provenance é a espinha epistêmica.** `inferred` nunca vira `observed`
   silenciosamente. Dados ausentes ficam `None` — não invente precisão.
3. **Núcleo puro.** Sem rede, wall-clock ou LLM calls em `core/`, `contracts/`,
   `otlp/`, `sanitize/`, `storage/`, `ingest/`. Rede só em `api/` e `sdk/exporter`.
4. **Sanitização por padrão.** Novos campos com conteúdo de prompt/tool devem
   respeitar `capture=metadata` → `full` é opt-in.
5. **Testes primeiro** quando possível: contrato → pipeline → API → e2e.

## Estrutura de um ciclo

O roadmap avança em ciclos com quality gates (ver `prompt_evo_inicial.md` e
`docs/cycles/`). Cada ciclo fecha com relatório incluindo escopo, evidências,
findings P0/P1/P2 e status (`VERIFIED`, `BLOCKED_EXTERNAL`, …).

## Commits e PRs

- Commits pequenos e incrementais — nada de PR gigante com o roadmap inteiro.
- Mensagens focadas no *porquê*.
- CI deve estar verde: pytest, ruff, mypy, tsc/node:test, pip-audit, build.
