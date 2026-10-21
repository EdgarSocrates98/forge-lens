# Guia — Investigação de falhas (§05.3 / §05.4 / §12.7)

> Estado: **backend implementado** em `src/forge_lens/investigate/` —
> `classify_failure` (taxonomia §05.3), `build_hypotheses` (§05.4) e
> `suggest_evaluators` (§12.7). Tudo determinístico, sobre evidência
> registrada — nenhuma categoria dispara sem sinal capturado.

## Classificação de falha

`classify_failure(trace) -> FailureReport` devolve `{trace_id, failed,
category, confidence, evidence, detail}`. `failed` é verdade quando o
trabalho errou, nunca completou (envelope fechado com spans abertos),
foi cancelado (sinal forte de cancelamento), ou a topologia declarada é
impossível. Flags de data-quality (`clock_skew`, `duplicate`, …) são
evidência, não veredicto.

### Taxonomia e sinais

As regras correm em **ordem de especificidade** — a primeira que casa
vence, e `evidence` nomeia `span_ids` + `signals` + `names` que
dispararam. Sinais fortes = fatos registrados (HTTP status em
`http.status_code`/`http.response.status_code`, `exception.type` em
attrs **e eventos** OTel, counters, flags booleanas); sinais fracos =
palavras-chave em texto livre (`status.message`, attrs `error.*`/
`exception.*`/`http.status_text`/`otel.status_description`, nomes de
eventos).

| Ordem | Categoria | Sinais que disparam |
|---|---|---|
| 1 | `cycle` | membro de ciclo de parentesco real (walk O(n) do graph builder) |
| 2 | `budget_exceeded` | `*.budget.exceeded`/`token_budget_exceeded` flags; texto quota/budget (`insufficient_quota`, `quota exceeded`, `out of credit`, …); **429 + termos de quota → budget, não rate_limit** |
| 3 | `auth_error` | `401`; `exception.type` auth (`AuthenticationError`, `UnauthorizedError`, …); texto `unauthorized`/`unauthenticated`/`invalid api key|token|credential`/`expired token`/`401` |
| 4 | `missing_permissions` | `403`; `PermissionError`; texto `forbidden`/`permission denied`/`access denied`/`insufficient permissions|scope`/`403` |
| 5 | `schema_mismatch` | `exception.type` schema/parse (`SchemaValidationError`, `JSONDecodeError`, `ParseError`); texto `schema`/`structured output`/`response_format`/`invalid json`/`failed to parse` |
| 6 | `invalid_arguments` | `400`/`422`; `exception.type` (`ValidationError`, `InvalidArgument`, `BadRequest`, `TypeError`, `ArgumentError`); texto `invalid argument|parameter|input`/`missing required …`/`bad request`/`validation error` |
| 7 | `retry_exhaustion` | counters `retry.count|attempts|attempt`, `http.retry_count`, `retries`, `attempt*` ≥ `retry.max*` registrado ou ≥ `RETRY_EXHAUSTION_THRESHOLD` (3); flags `retry.exhausted`/`retries_exhausted`; ≥3 eventos `retry`; `exception.type` `*Retry*`; texto `max retr…`/`retries exhausted`/`gave up` |
| 8 | `rate_limit` | `429` puro (sem termos de quota); texto `rate limit`/`too many requests` — **só em spans que erraram** (429 recuperado não é causa) |
| 9 | `timeout` | `exception.type` `*Timeout*`/`Deadline*`; texto `timeout`/`timed out`/`deadline` ("exceeded" nu não basta) |
| 10 | `agent_cancelled` | `exception.type` `*Cancel*`/`*Abort*` (asyncio.CancelledError, …); flags `cancelled`/`canceled`/`aborted`/`agent.cancelled`; `status.message` com `cancel…`/`abort…` |
| 11 | `guardrail` → `tool_error` → `llm_error` | erro em span do kind, sem sinal mais específico |
| — | `agent_cancelled` (não-errored) | sinal forte de cancelamento em span sem `error` — marca `failed=true` |
| 12 | `incomplete` | spans `missing_end` com envelope fechado e nenhum erro (`confidence=inferred`) |
| 13 | `unknown` | erros sem assinatura reconhecível (`confidence=inferred`); fallback honesto — nunca inventado |

Notas de ordem importantes:

- **`budget_exceeded` antes de `rate_limit`**: um 429 cuja mensagem
  nomeia quota/budget é exaustão de conta — o trottle transitório fica
  para o 429 puro.
- **`schema_mismatch` antes de `invalid_arguments`**: "schema validation
  failed" é mais específico que um 400 nu; `ValidationError` sem
  contexto de schema cai em `invalid_arguments`.
- **`agent_cancelled` depois das causas**: cancelamento costuma ser
  *como* outra causa se manifesta (deadline/budget disparam o cancel) —
  as concorrentes ficam visíveis nas hipóteses.
- Sinais fracos de cancelamento (`status_message`) só contam em spans
  que **erraram** — "order was cancelled" num span saudável é dado de
  negócio, não run cancelada.

## Hipóteses de causa-raiz (§05.4)

`build_hypotheses(trace, report=None, max_hypotheses=10) -> list[Hypothesis]`
emite uma hipótese por categoria com sinais — a primária **e** as
concorrentes — ordenadas deterministicamente. Campos:

| Campo | Conteúdo |
|---|---|
| `hypothesis_id` | `"{trace_id}:{categoria}"` — determinístico |
| `label` | categoria defendida |
| `symptom` | descrição do candidato (primário/concorrente, spans) |
| `supporting_evidence` | `"span_id: sinal"` por sinal observado |
| `contradictory_evidence` | sinais de outras categorias apontando para outra explicação (bounded) |
| `missing_evidence` | o que confirmaria mas não foi capturado (ex.: "response body not captured — set capture=full") |
| `suggested_verification` | passos concretos de verificação por categoria |
| `confidence` | `observed` (sinal forte) / `inferred` (só texto fraco) |
| `status` | `proposed` (1 sinal) / `supported` (≥2 sinais ou spans) / `refuted` (somente com evidência contraditória: suporte fraco + rival forte no mesmo span) / `unverifiable` (falha sem assinatura) |

Trace saudável → `[]`; falha sem assinatura → uma hipótese `unknown`
`unverifiable` nomeando os gaps de captura.

## Sugestões de avaliação (§12.7)

`suggest_evaluators(reports) -> {"suggestions": [...], "note": …}`
mapeia categorias de falha — uma trace ou várias (a view recorrente:
`[classify_failure(t) for t in traces]`) — para specs de avaliador e
esboços de dataset-case: `{kind, name, params, params_required,
rationale, evidence: {traces, count, recurring, categories},
classification: "candidate"}`. Ex.: `invalid_arguments` recorrente num
tool → `allowed_tools` com `allowed=[nomes observados]` + dataset case
`invalid_args_rejected`; `timeout` → `latency_budget` (`params_required:
["max_duration_ms"]` — SLOs ficam com o revisor); `budget_exceeded` →
`token_budget`; `retry_exhaustion` → `tool_call_count` com `max=3`.
`unknown`/saudável não gera nada — **datasets nunca são modificados**,
sugestões são sempre candidatas.

## Onde o output aparece

- `GET /v1/traces/{id}/failure` — o `FailureReport` puro.
- `GET /v1/traces/{id}/incident` — bundle com `hypotheses` +
  `eval_suggestions`.
- Specialist `failure_investigator` (`POST /v1/analyze/…`) — findings
  com report + `hypotheses` (cap `context.max_hypotheses`, default 5;
  `hypothesis_count`/`hypotheses_truncated` declaram o corte) +
  `eval_suggestions`.
- `forge-lens incident <id>` — bundle completo no CLI.
