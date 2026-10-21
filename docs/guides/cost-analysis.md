# Guia — Análise de Custo e Tokens

> Estado: **backend implementado** (ciclo 04) em `src/forge_lens/intel/` —
> ver `docs/cycles/cycle-04-final-report.md`.

## O que está implementado

- `TokenUsage` por span `llm` (input/output/cached/reasoning/total +
  `measurement_source`). Visível em `traces show` (`~ tokens:`), na API e
  nos fixtures — medido do provider quando reportado, nunca estimado
  silenciosamente.
- **Pricing registry versionado** (`intel/pricing.py`): `ModelPrice` +
  `PricingRegistry` carregável de arquivo; preço não-encontrado vira
  `unknown`, não zero silencioso.
- **`GET /v1/traces/{id}/cost`** (`intel/cost.py`): `CostReport` por span
  e total — tokens observados × tarifa do registry.
- **`GET /v1/analytics/pricing`** — expõe a tabela ativa e sua versão.
- **Waste detection** (`intel/waste.py`): `detect_waste` emite
  `WasteFinding` com evidência — retries com mesmo `tool_args_sha256`,
  spans `duplicate`/`conflict` com tokens gastos, etc.
- CLI `forge-lens cost <trace_id>` — relatório de custo derivado.

## Regras epistêmicas de custo

- **`estimated_cost` ≠ `billed_cost`**: estimado vem de um registry de
  preços (`derived` + `assumptions[]` + `pricing_version`); faturado só
  quando o provider reporta (`observed`).
- **`measurement_source`**: `provider` (real), `estimated` (tokenizador
  local — declare!), `unknown`. Contador ausente = `None`.
- Agregações mostram a fonte: "total ≈ R$ X (estimated, openai-pricing-v3)"
  nunca "custo X" a seco.

## Roadmap (ciclos seguintes)

- Rollups por agente/sessão/projeto/janela (hoje: por-trace).
- Comparação de custo entre experimentos via `POST /v1/eval/compare`
  (métricas de custo ainda não são evaluators; ciclo 06 entregou o
  framework, não o evaluator de custo).
- Detecção de desperdício contextual adicional (cached vs. re-computado).

## Hoje, na mão

```bash
forge-lens cost <trace_id>
forge-lens traces show <id> --json | \
  jq '[.spans[].attributes["forge_lens.token_usage"] // empty]'
```
