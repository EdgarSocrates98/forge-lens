# Guia — Análise de Custo e Tokens

## O que está implementado

`TokenUsage` por span `llm` (input/output/cached/reasoning/total +
`measurement_source`). Visível em `traces show` (`~ tokens:`), na API e
nos fixtures — medido do provider quando reportado, nunca estimado
silenciosamente.

## Regras epistêmicas de custo

- **`estimated_cost` ≠ `billed_cost`**: estimado vem de um registry de
  preços (`derived` + `assumptions[]` + `pricing_version`); faturado só
  quando o provider reporta (`observed`).
- **`measurement_source`**: `provider` (real), `estimated` (tokenizador
  local — declare!), `unknown`. Contador ausente = `None`.
- Agregações mostram a fonte: "total ≈ R$ X (estimated, openai-pricing-v3)"
  nunca "custo X" a seco.

## Roadmap (ciclos seguintes)

- Registry de preços versionado (`docs/adr/` registrará a fonte de cada tabela).
- Rollups por agente/sessão/projeto/janela.
- Comparação de custo entre experimentos (ligado a evaluations, ciclo 07).
- Detecção de desperdício contextual (cached vs. re-computado).

## Hoje, na mão

```bash
forge-lens traces show <id> --json | \
  jq '[.spans[].attributes["forge_lens.token_usage"] // empty]'
```
