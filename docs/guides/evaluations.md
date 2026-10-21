# Guia — Evaluations

> Estado: **backend implementado** (ciclo 06) em `src/forge_lens/eval/` —
> ver `docs/cycles/cycle-06-final-report.md` e `docs/reference/api.md`.

## O que existe hoje

**Evaluators built-in** (`EVALUATORS` em `eval/evaluators.py`):

| Nome | Métrica | Notas |
|---|---|---|
| `error_free` | 1.0 se nenhum span `error` | binário |
| `tool_success` | fração de tool spans sem erro | `score=None` sem tool spans |
| `latency_budget` | 1.0 se envelope ≤ `max_duration_ms` | params: `max_duration_ms` >0 finito (`budget_ms` segue aceito — nome legado do CLI); sem `ended_at` → `score=None` ("unended trace — duration unknown") |
| `token_budget` | 1.0 se tokens observados ≤ `max_total_tokens` | caveat declara spans sem usage |
| `has_span` | 1.0 se existe span com `name` dado | params: `name` |
| `exact_match` | 1.0 se `field` observado == `expected` | `score=None` se campo não observado |
| `token_f1` | F1 bag-of-tokens entre `expected` e `field` | contínuo — Mann-Whitney U no compare |
| `allowed_tools` | 1.0 se tool names ⊆ `allowed` | nome semântico: `tool.name` ou span name |
| `task_completion` | 1.0 se trace terminou (sem erro se `require_no_error`) | binário |
| `contains` | 1.0 se `text` aparece na saída observada | params: `text`, `case_sensitive` (default true), `field` (default `output.value`); `score=None` se campo não observado como string |
| `regex` | 1.0 se `pattern` casa na saída observada | params: `pattern`, `case_sensitive`, `field`; padrão inválido → `evaluator_error` (nunca exception); `match` reportado truncado em ~120 chars |
| `tool_call_count` | 1.0 se nº de TOOL spans ∈ [`min`, `max`] | params: `min`, `max` int ≥0; `tool` opcional conta só spans com atributo `tool.name` igual; `min > max` → `evaluator_error` |

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
- **Comparação honesta**: cada métrica declara seu teste no campo
  `test` e nenhum p-value é fabricado. Binárias (`test =
  "two_proportion_z"`) — z-teste de duas proporções sobre o
  `pass_rate` + intervalo de Wilson a 95% por lado em `ci_a`/`ci_b`
  (Wilson mantém cobertura honesta em n pequeno e nas bordas 0/1, e é
  reportado mesmo abaixo do piso — descritivo, não teste). Contínuas
  (`test = "mann_whitney_u"`) — teste de Mann-Whitney U sobre os
  scores brutos por resultado: aproximação normal com correções de
  continuidade e de empates, mesmo piso de n≥10 por lado; abaixo disso
  é recusado (`insufficient_data`), nunca aproximado em silêncio.
  `not_computable` quando a variância é zero (p indefinido — ex.
  scores todos empatados) e `not_tested` quando o avaliador não está
  no registry — semântica do score não verificada.
- **Limites honestos do compare**: MWU detecta deslocamento de
  distribuição (ordem estocástica), não delta de média/mediana — a
  direção usada pelo gate vem do `delta` de média observado. Amostras
  são por-resultado: um spec que repete a mesma métrica numa trace
  gera resultados não-independentes (convenção do `n` da summary).
  Quando >1 métrica é testada, o relatório emite
  `multiple_comparisons` ("k tests at alpha=0.05 unadjusted —
  interpret jointly with care") — não há correção de
  multiplicidade; k testes a α=0.05 devem ser lidos em conjunto.
  `fail_on_significant_regression` agora alcança também regressões
  contínuas significativas (MWU + `delta` < 0).
- **Bounds no boundary**: JSON inválido → 400; shape errado → 422;
  corpos streamed com cap; trace_ids/specs rejeitados acima do cap
  (não truncados silenciosamente).

## Não implementado (roadmap)

- **Datasets persistidos/ground truth** — `EvaluationResult` já carrega
  `dataset`/`ground_truth_reference`, mas não há CRUD de datasets;
  evals rodam sobre traces armazenadas.
- **CI evaluation gates** — bloquear merge por regressão de métrica.
- **Evaluators custom/declarativos definidos pelo usuário** — hoje o
  registry é built-in; specs com nome desconhecido degradam para
  `score=None` + reason, não para extensão.
