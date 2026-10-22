# Guia — Evaluations

> Estado: **backend implementado** (ciclo 06; 17 avaliadores após a
> wave do ciclo 13) em `src/forge_lens/eval/` — ver
> `docs/cycles/cycle-06-final-report.md`,
> `docs/cycles/cycle-13-improvement-wave.md` e `docs/reference/api.md`.

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
| `schema_validation` | 1.0 se o valor JSON de `field` satisfaz `schema` | params: `schema` (obrigatório), `field` (default `output.value`). Subconjunto JSON-Schema honesto: apenas `type` (`object`/`array`/`string`/`number`/`integer`/`boolean`/`null`), `properties`, `required`, `items` — qualquer outro keyword (`minimum`, `additionalProperties`, `enum`, …) → `evaluator_error`, nunca ignorado em silêncio. String que parseia como objeto/array JSON é validada como a estrutura; string não-parseável (ou escalar) valida como string — `"42"` continua string. Campo não observado → `score=None`. `integer` aceita float integral (2.0); `bool` nunca é número; aninhamento >32 níveis → `evaluator_error` |
| `evidence_coverage` | 1.0 se cobertura de atributos ≥ `min_coverage` | params: `required_attrs` (lista não-vazia de chaves), `min_coverage` ∈ (0,1] (default 1.0). Semântica trace-level: atributo `X` é coberto quando ≥1 span (ou `resource_attributes`) tem chave `X` ou sob o namespace `X.` (`gen_ai.usage` cobre `gen_ai.usage.input_tokens`; `tool` não cobre `toolshed`). Score é o veredito binário do threshold (pass_rate/z-teste corretos); a fração crua vai em `details.coverage` |
| `human_review` | sempre `score=None` | marcador de fila: `metric=pending_human_review`, `details.reason=pending_human_review`, `verdict=neutral`, `rubric` ecoado — nenhum score fabricado; cai em `nulls` na summary |
| `precision_recall` | F1 entre conjunto observado e `expected_items` | params: `expected_items` (lista não-vazia) + exatamente um modo: `tool: true` (conjunto de tool names invocados — `tool.name` ou nome do span) ou `field: "<attr>"` (extrai itens: lista→elementos, dict→chaves, string-JSON→desempacota, escalar→singleton). Comparação por forma JSON canônica (`1` ≠ `"1"`). Campo não observado → `score=None`; conjunto observado vazio (ex.: zero tool calls) é evidência real → 0.0. Contínuo — MWU no compare |
| `claims_grounded` | 1.0 se overlap lexical da saída com fontes ≥ `min_overlap` | params: `required_source_attr` (ex.: `tool.output`, `retrieval.documents`), `min_overlap` ∈ (0,1] (default 0.5), `field`. overlap = \|tokens da saída ∩ tokens das fontes\| / \|tokens da saída\| (multiset, mesmo `_WORD_RE` de `token_f1`). **Heurística documentada, não checagem factual**: bag-of-words sem stopwords, sem paráfrase/negação; fontes estruturadas contribuem chaves+valores como tokens. Sem fonte observada → `score=None` ("not checkable") — em `capture=metadata` atributos de conteúdo são dropped no ingest, ausência é não-observabilidade, nunca prova de claim não suportado |

**Endpoints** (`api/app.py`):

- `GET /v1/traces/{trace_id}/eval` — avalia uma trace com os defaults.
- `POST /v1/eval/run` — `{trace_ids: [≤500], evaluators: [{name, params?}, ≤100], dataset?, project_id?}`.
- `POST /v1/eval/compare` — `{a: {trace_ids|project_id}, b: {...}}`; side
  pode declarar `project_id` próprio (A/B cross-project explícito).

**Gate de CI** (`scripts/eval_gate.py`): gate determinístico de
regressão sobre traces armazenadas — mesma cadeia
`evaluate_traces` → `compare_runs` → `apply_gate` dos endpoints, rodável
offline (`python scripts/eval_gate.py`, stdlib + forge_lens). Saída JSON
determinística no stdout, resumo humano no stderr, exit codes 0/1/2/64/70
que distinguem pass, regressão com evidência, dados insuficientes, erro
de uso e falha interna. Contrato completo em `docs/eval-gate.md`;
workflow GitHub Actions copiável em
`docs/eval-gate-workflow-example.yml`.

**Datasets** (`storage/sqlite.py` + `api/app.py`): CRUD completo —
`PUT/GET /v1/datasets/{id}`, `PUT/GET /v1/datasets/{id}/versions/{v}`,
listagens; versões são append-only e carregam cases
(`input`/`expected`/`allowed_tools`/`constraints`/`rubric`) + metadata.
`EvaluationResult.dataset`/`ground_truth_reference` já são populados.
O mapeamento case→spec de avaliador **não é inventado pelo engine** —
convenção do gate: a bateria fica em `metadata.evaluators` do dataset
(ou da versão).

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
- **Cobertura §06.2/§06.5**: `schema_validation` implementa um
  subconjunto explícito de JSON Schema (`type`/`properties`/`required`/
  `items`) — keywords fora do subconjunto são `evaluator_error`, nunca
  relaxadas em silêncio. `evidence_coverage` mede presença trace-level
  de atributos (prefixo com boundary de `.`); per-span foi rejeitado
  porque evidência de agente vive distribuída entre spans. `human_review`
  é um marcador de fila (`score=None` sempre) — o veredito humano entra
  por outro caminho, não por score fabricado. `precision_recall` mede
  F1 de conjunto (tools invocadas ou itens do campo) — conjunto
  observado-vazio é falha mensurável, campo não-observado é `None`.
  `claims_grounded` é a aproximação determinística honesta de
  "unsupported claims": overlap lexical bag-of-words entre a saída e
  fontes observadas — **não** detecta claims facticamente falsos que
  reusam vocabulário da fonte, nem perdoa paráfrases fiéis; sem fonte
  observada retorna `None`, nunca fail fabricado.

## Sugestões de cenários a partir de falhas (§12.7)

`forge_lens.investigate.suggest_evaluators(reports)` mapeia categorias
de `FailureReport` — de uma trace ou de muitas
(`[classify_failure(t) for t in traces]` para a view *recorrente*) —
para specs de avaliador e esboços de dataset-case:
`{kind, name, params, params_required, rationale, evidence,
classification: "candidate"}`. O `evidence` carrega `traces`, `count` e
`recurring` (≥2 traces distintas). Mapeamentos:

| Categoria recorrente | Sugestão |
|---|---|
| `invalid_arguments` | `allowed_tools` (`allowed` = nomes observados) + dataset case `invalid_args_rejected` |
| `schema_mismatch` | `regex`/`pattern` guard no output + case `schema_conformance` |
| `timeout` / `incomplete` / `agent_cancelled` | `latency_budget` (`max_duration_ms` — `params_required` fica com o revisor), `task_completion` |
| `budget_exceeded` / `llm_error` | `token_budget` (`max_total_tokens`) + case `budget_pressure` |
| `retry_exhaustion` | `tool_call_count` `{min:0, max:3}` + `error_free` |
| `rate_limit` / `auth_error` / `missing_permissions` | `error_free` + dataset case do cenário (`provider_throttle`, `expired_or_invalid_credential`, `insufficient_scope`) |
| `tool_error` / `guardrail` | `tool_success`, `allowed_tools` |
| `cycle` | dataset case `parentage_integrity` |

`unknown` e traces saudáveis não geram sugestão — recomendar eval para
causa desconhecida seria inventar o que se recusou a classificar.
**Datasets nunca são modificados** — as sugestões são `candidate`,
revisadas e aplicadas à mão; aparecem no bundle `/incident` e nos
findings do specialist `failure_investigator`. Detalhe completo em
`docs/guides/failure-investigation.md`.

## Não implementado (roadmap)

- **Case→spec mapping automático** — datasets persistem cases
  (`input`/`expected`/`allowed_tools`/`constraints`/`rubric`), mas o
  engine nunca inventa qual avaliador verifica qual case; a bateria é
  declarada em `metadata.evaluators` (convenção do eval-gate) e os
  campos `expected`/`allowed_tools`/`rubric` são ainda payload
  armazenado, não params derivados.
- **Evaluators custom/declarativos definidos pelo usuário** — hoje o
  registry é built-in; specs com nome desconhecido degradam para
  `score=None` + reason, não para extensão.
- **LLM-as-judge** — fora do engine por regra (núcleo determinístico,
  sem chamadas de LLM); o spec §06.2 o lista como opcional e exige que
  nunca seja a única evidência — os evaluators determinísticos acima
  são esse primeiro sinal. Uma integração futura viveria fora de
  `eval/` (boundary), não no core.
- **Fila de human review** — `human_review` emite o marcador
  `pending_human_review`; a UI/fila que consome o marcador e registra
  o veredito humano não existe ainda.
- **Detecção factual de unsupported claims** — `claims_grounded` cobre
  o sinal lexical; grounding factual (entailment) não é verificável
  deterministicamente e não foi fabricado.
