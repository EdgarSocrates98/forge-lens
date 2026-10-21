# Guia — Replay

> Estado: **observed replay implementado** (ciclo 05) — leitura
> determinística do trace via `/v1/traces/{id}/replay` e CLI. Simulação e
> re-execução são roadmap (ciclo 05 spec, não implementado) —
> `DOCUMENTED_ONLY` abaixo.

## Os três modos (definições do produto)

| Modo | O que é | Estado |
|---|---|---|
| **Observed Replay** | Reconstrução read-only dos eventos gravados | ✅ hoje (CLI/API) |
| **Simulation Replay** | Re-execução explícita com artefatos registrados | roadmap |
| **Re-execution** | Nova execução real, autorizada e com política | roadmap |

## O que o produto **nunca** faz

- **Nunca re-executa ferramentas ao abrir um trace.** Abrir `traces show`
  é operação read-only — sem side effects, sem chamadas a MCP/LLM/FS.
- Nunca mistura evento observado com evento simulado sem rótulo:
  `provenance` distingue `observed` de qualquer coisa sintética.

## Simulation Replay (proposta — ciclo 05 spec, não implementado)

- Entradas: trace + artefatos registrados (`BlobRef`) + política declarativa.
- Saída: execução *rotulada* `provenance=inferred`/nova trace vinculada por
  `SpanLink` à original — nunca sobrescrita.
- Controles: allowlist de ferramentas, dry-run, timeout, sem rede por default.

## Re-execution (proposta)

Somente com política explícita do operador (allowlist, aprovação, ambiente
isolado). Fora de escopo até governança existir.
