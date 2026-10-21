# ADR-0001 — Contratos versionados via Pydantic + JSON Schema

**Status**: ACCEPTED (ciclo 00)

## Contexto

Forge Lens precisa de uma fronteira de compatibilidade estável entre
produtores (SDKs, OTLP, adaptadores), o servidor e futuros consumidores
(UI, exporters, evals). Sem contrato, cada módulo drifta e o "formato" vira
o que o código faz por acidente.

## Decisão

- Modelos Pydantic em `src/forge_lens/contracts/v1/` — fonte única de verdade.
- `schema_version` explícito em todo modelo; `extra="allow"` preserva campos
  desconhecidos (forward-compat sem perda).
- JSON Schemas **gerados** (`contracts/jsonschema.py`) e commitados em
  `schemas/v1/` — consumidores validam sem instalar o pacote.
- CI falha se `schemas/v1/` divergir dos modelos.

## Consequências

- Qualquer quebra exige bump de `schema_version` — decisão explícita, nunca
  acidental.
- TypeScript SDK espelha os tipos à mão (zero-dep) — fixtures golden e o
  envelope compartilhado mantêm paridade verificável.
- Custo: disciplina de regenar schemas; mitigado pelo check no CI.

## Alternativas rejeitadas

- **dataclasses/attrs soltos**: sem validação/serialização JSON pronta.
- **protobuf-first**: traria toolchain; OTLP JSON já cobre o fio.
- **JSON Schema hand-written**: duas fontes de verdade — drifto garantido.
