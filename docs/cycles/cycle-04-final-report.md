# Cycle 04 — Relatório Final: Inteligência de Tokens, Custo e Contexto

**Status**: `PARTIAL` — engine implementada; dashboards/agregações de
longo prazo dependem de Cycle 02 (UI) e Cycle 09 (backends/benchmarks)
**Data**: 2026-10-21

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| `PricingRegistry` — tabela bundled + override `pricing_file` JSON, lookup exato + prefixo | `src/forge_lens/intel/pricing.py` | VERIFIED |
| `CostReport`/`SpanCost`/`trace_cost` — custo derivado por span, por modelo, total | `src/forge_lens/intel/cost.py` | VERIFIED |
| `detect_waste` — 5 heurísticas evidenciadas | `src/forge_lens/intel/waste.py` | VERIFIED |
| Endpoints `/v1/traces/{id}/cost`, `/v1/analytics/pricing` | `src/forge_lens/api/app.py` | VERIFIED |
| Config `pricing_file` + `FORGE_LENS_PRICING_FILE` | `src/forge_lens/core/config.py` | VERIFIED |
| CLI `forge-lens cost <trace_id>` | `src/forge_lens/cli/main.py` | VERIFIED |
| Testes unitários + API | `tests/unit/test_graph_intel.py`, `tests/integration/test_api.py` | VERIFIED |

## 2. Regras epistêmicas (inalienáveis)

- **`derived`, nunca `observed`** — `CostReport.provenance = "derived"`;
  estimativas carregam `pricing_source` + `assumptions` (ex.: modelo
  resolvido por prefixo, contadores ausentes tratados como zero,
  reasoning faturado como output, cached sem tarifa própria → input).
- **`None` ≠ zero** — `estimated_usd` é `null` quando o modelo não está
  no registry; o span entra em `unpriced_models`, nunca com custo
  inventado. Contadores de token ausentes ficam `null` por span.
- **Contadores observados apenas** — totais somam o que o produtor
  reportou via `forge_lens.token_usage` (SDK `record_usage` ou OTLP
  OpenInference); spans LLM sem usage entram em
  `usage_missing_on_llm_spans` + finding `unknown_tokens` — lacuna
  honesta, não zero silencioso.

## 3. Pricing registry

- Tabela bundled (`PRICING_SOURCE_ID = "forge-lens-bundled-2026-01"`):
  OpenAI (gpt-4o/4o-mini/4.1 family/o3/o4-mini) e Anthropic (claude
  opus-4/sonnet-4/3.7/3.5/haiku) — preços de lista públicos; **sempre**
  identificada como fonte nas estimativas.
- `pricing_file` (JSON) substitui a tabela inteira; `source` passa a
  ser `file:<nome>` — visível em toda resposta e no relatório.
- Lookup: exato → prefixo mais longo em fronteira de `-`
  (`gpt-4o-2024-08-06` → `gpt-4o`).

## 4. Heurísticas de desperdício (`waste[]`)

| Kind | Evidência | USD |
|---|---|---|
| `retry_amplification` | ≥3 invocações tool/LLM com mesmo nome + fingerprint de args sob um parent | — |
| `context_replay` | `Σinput − max(input) ≥ max(input)` — contexto reenviado integralmente | — |
| `duplicate_delivery` | spans com flag `duplicate`/`conflict` que carregam token_usage | quando precificável |
| `unknown_tokens` | spans `kind=llm` sem `forge_lens.token_usage` | — |
| `unpriced_model` | modelos com usage fora do registry | — |

Fingerprint de args = sha256 do blob canônico (`forge_lens.tool_args`,
`tool.parameters`, `tool_call.arguments`) — comparação de conteúdo, não
de referência.

## 5. O que NÃO está neste ciclo (honesto)

- Agregação custo-por-projeto/janela temporal e dashboards — requer
  varredura do store além de uma trace; previsto para Cycle 09
  (analytics de longo prazo sobre o backend escalável).
- `billed_cost` — nenhum produtor reporta custo faturado hoje; o campo
  do contrato `CostObservation` permanece reservado para quando existir
  observação real (estimativa nunca é escrita como billed).
- Estimador de tokens local (tokenizer) — `measurement_source=estimated`
  existe no contrato, mas nenhum estimador foi implementado: preferido
  não fabricar contagens.
- Recomendações de otimização acionáveis — Cycle 12.

## 6. Evidência

- `test_cost_report_exact_pricing_math` (600×4.0 + 400×1.0 + 100×8.0),
  `test_cost_cached_and_reasoning_rates`,
  `test_cost_report_prefix_resolution_and_unpriced`,
  `test_cost_report_flags_llm_spans_without_usage`.
- `test_waste_*` — 4 findings exercitados.
- `test_cost_endpoint_priced_and_waste` — `pricing_file` atravessando a
  app real + `GET /v1/analytics/pricing`.
- `forge-lens cost <id>` imprime tokens, por-modelo, unpriced e waste.
