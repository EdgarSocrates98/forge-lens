# Guia — Evaluations

> Estado: **backend implementado** (ciclo 06) em `src/forge_lens/eval/` —
> ver `docs/cycles/cycle-06-final-report.md` e `docs/reference/api.md`.

## O que existe hoje

**Evaluators built-in** (`EVALUATORS` em `eval/evaluators.py`):

| Nome | Métrica | Notas |
|---|---|---|
| `error_free` | 1.0 se nenhum span `error` | binário |
| `tool_success` | fração de tool spans sem erro | `score=None` sem tool spans |
| `latency_budget` | 1.0 se envelope ≤ `budget_ms` | params: `budget_ms` finito |
| `token_budget` | 1.0 se tokens observados ≤ `max_total_tokens` | caveat declara spans sem usage |
| `has_span` | 1.0 se existe span com `name` dado | params: `name` |

**Endpoints** (`api/app.py`):

- `GET /v1/traces/{trace_id}/eval` — avalia uma trace com os defaults.
- `POST /v1/eval/run` — `{trace_ids: [≤500], evaluators: [{name, params?}, ≤100], dataset?, project_id?}`.
- `POST /v1/eval/compare` — `{a: {trace_ids|project_id}, b: {...}}`; side
  pode declarar `project_id` próprio (A/B cross-project explícito).

## Semântica honesta

- **`score=None` nunca é zero**: param inválido, spec malformado, trace
  incompleta ou duração negativa → `score=None` + `details.reason`. A
  summary conta `n` (scored) e `nulls` separadamente.
- **Determinístico / wall-clock-free**: `evaluated_at` só aparece quando
  um `now` é injetado — relatórios são byte-idênticos entre execuções.
- **Comparação honesta**: z-test de duas proporções só para métricas
  binárias com n≥10 por lado; senão `insufficient_data` (sub-powerado),
  `not_computable` (variância zero — p indefinido) ou `not_tested`
  (métrica não-binária). Nenhum p-value é fabricado.
- **Bounds no boundary**: JSON inválido → 400; shape errado → 422;
  corpos streamed com cap; trace_ids/specs rejeitados acima do cap
  (não truncados silenciosamente).

## Não implementado (roadmap)

- **Datasets persistidos/ground truth** — `EvaluationResult` já carrega
  `dataset`/`ground_truth_reference`, mas não há CRUD de datasets;
  evals rodam sobre traces armazenadas.
- **CI evaluation gates** — bloquear merge por regressão de métrica.
- **Evaluators custom/declarativos** (exact-match, schema-check) — hoje
  o registry é built-in; specs com nome desconhecido degradam para
  `score=None` + reason, não para extensão.
