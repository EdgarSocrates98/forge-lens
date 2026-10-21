# Cycle 06 — Relatório Final: Engine de Avaliação

**Status**: `PARTIAL` — engine determinístico de avaliação implementado
(avaliadores sobre evidência, relatório agregado, comparação A/B com
teste estatístico honesto); datasets versionados com ground-truth
persistido, eval-driven-regression em CI e a UI de experimentos dependem
de Cycles 02/07/11.
**Data**: 2026-10-21

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| Avaliadores determinísticos (`error_free`, `tool_success`, `latency_budget`, `token_budget`) | `src/forge_lens/eval/evaluators.py` | VERIFIED |
| `EvalReport` + `evaluate_traces` — batch ordenado + summary agregado | `src/forge_lens/eval/runner.py` | VERIFIED |
| `compare_runs` — deltas por métrica + z-teste de proporções | `src/forge_lens/eval/compare.py` | VERIFIED |
| Endpoints `GET /v1/traces/{id}/eval`, `POST /v1/eval/run`, `POST /v1/eval/compare` | `src/forge_lens/api/app.py` | VERIFIED |
| CLI `forge-lens eval <trace_id>` | `src/forge_lens/cli/main.py` | VERIFIED |
| Testes unitários + API | `tests/unit/test_graph_intel.py`, `tests/integration/test_api.py` | VERIFIED |

## 2. Semântica honesta

- **Determinístico**: nenhuma chamada de LLM, nenhum relógio.
  `evaluated_at` é preenchido somente quando o caller injeta `now` —
  inputs idênticos produzem relatórios byte-idênticos.
- **`score=None` é resposta válida**: trace incompleto, sem spans do
  tipo avaliado, parâmetro faltando ou avaliador desconhecido emitem
  `score=None` + `details.reason` — nunca um veredito fabricado.
- **Significância nunca é fabricada**: o z-teste de duas proporções só
  roda em métricas binárias com n≥10 por lado; métricas contínuas →
  `not_tested`; variância pooled zero → `not_computable`; amostra
  pequena → `insufficient_data`. `p_value` fica `null` em todos esses
  casos.
- **`provenance=derived`** em todo `EvaluationResult` — scores são
  derivados de evidência, nunca observação.
- Toda coleção emitida é ordenada (traces por `trace_id`, summary por
  chave `evaluator/metric`).

## 3. Exclusões honestas (não implementado)

- **Datasets versionados com ground-truth**: o contrato
  `EvaluationResult` já tem `dataset`/`ground_truth_reference`, mas não
  há store de datasets nem evaluators de exact-match/F1 sobre
  ground truth.
- **Evaluators estatísticos de saída** (similaridade, format checks):
  os 4 atuais cobrem saúde de execução e budgets, não qualidade de
  resposta.
- **CI gate de regressão de eval** (comparar run vs baseline em PR):
  o backend `compare_runs` existe; a automação depende de Cycle 11.
- **UI de experimentos**: depende de Cycle 02.
