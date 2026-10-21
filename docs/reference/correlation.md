# Referência — Correlation (§05.5)

`forge_lens.intel.correlation` relaciona os sinais que hoje só são
observados isolados: execução ↔ falhas de tool ↔ chamadas de modelo ↔
crescimento de contexto ↔ retries ↔ scores de eval. É a peça que o
`incident` agrega por cima — aqui ficam as associações brutas, cada
uma com evidência (span ids) e força (contagens/razões reais).

> **Correlação não é causalidade.** Tudo aqui é associação
> *descritiva*: precedência temporal dentro de um trace e diferenças
> de taxa dentro de uma janela limitada. Nenhuma linha afirma
> mecanismo e nenhuma diferença é testada quanto a significância —
> veredito estatístico é trabalho de `/v1/eval/compare`.

## API

```python
from forge_lens.intel.correlation import correlate_trace, correlate_fleet

correlate_trace(trace: Trace) -> dict[str, Any]
correlate_fleet(store: TraceStore, project_id: str | None, *,
                limit: int = 200) -> dict[str, Any]
```

`correlate_trace` é pura sobre o contrato `Trace` (mesma forma de
`trace_efficiency`/`recommend` — a rota resolve o trace e devolve o
dict). `correlate_fleet` recebe o `TraceStore` e o `project_id`
(`None` = wildcard todos os projetos), lista a janela com
`list_traces` e hidrata cada trace via `get_trace` com o
`project_id` da própria linha — o mesmo fluxo de
`/v1/intel/efficiency`.

## `correlate_trace` — associações intra-trace

Retorna `{trace_id, correlations, signal_inventory, classification,
note}`. Cada correlação:

```json
{
  "kind": "retries_preceded_error",
  "classification": "temporal_association",
  "signals": [
    {"type": "prior_invocations", "span_ids": ["…"], "value": 3},
    {"type": "error_spans", "span_ids": ["…"], "value": 1}
  ],
  "strength": {"preceding_invocations": 3, "error_invocations": 1,
               "declared_retry_attrs": 0},
  "evidence_span_ids": ["…"],
  "caveat": "…",
  "detail": "…"
}
```

`signals` agrupa os span ids por papel; `strength` são sempre números
observados (nunca scores inventados); `evidence_span_ids` é a união
ordenada por posição no trace. `classification` é sempre
`temporal_association` — precedência por `start_time` apenas.

| kind | dispara quando | força reportada |
|---|---|---|
| `retries_preceded_error` | ≥1 invocação mesmo nome+kind antes de span de erro (tool/llm) | invocações anteriores, invocações com erro, retry counters declarados |
| `tool_failure_cluster` | ≥2 invocações com erro do mesmo `tool.name` | falhas / invocações / `failure_share` |
| `model_switch` | ≥2 labels de modelo distintos entre spans LLM | modelos distintos, chamadas, chamadas sem label |
| `context_growth_preceded_error` | maior salto de `input_tokens` entre chamadas LLM consecutivas > 0 e erro começando depois | `+Δ` tokens, de/para, erros posteriores |
| `context_growth_preceded_eval` | mesmo salto + score de avaliador observado depois | `+Δ` tokens, n de scores, min/max |
| `errors_preceded_eval_score` | spans de erro antes de span EVALUATOR com score | erros anteriores, n de scores, min/max |

Convenções de extração (todas "observado ou ausente", nunca
adivinhadas): nome semântico de tool = `tool.name` se presente, senão
o nome do span (mesma convenção dos avaliadores); modelo =
`forge_lens.token_usage.model`, senão `llm.model_name` /
`gen_ai.request.model` / `gen_ai.response.model`; retries declarados
= `gen_ai.tool.call.retry_count` ou `retry.count`; scores = atributos
numéricos em spans `kind=evaluator` (`eval.score`,
`evaluation.score`, `forge_lens.eval.score`, `forge_lens.score`,
`score` ou qualquer chave `*.score`).

Trace sem nada a parear → `correlations: []` + `signal_inventory`
honesto (contagens por kind, modelos/tools observados).

## `correlate_fleet` — co-ocorrência em janela limitada

Janela = `limit` traces mais recentes, teto rígido de **500** (mesma
disciplina de `/v1/ops/summary`). Retorna `{project_id, window,
outcomes, tables, trace_signals, classification, note}` — o
`classification` do topo é `descriptive_co_occurrence` e
`trace_signals` traz a linha de features por trace (a evidência por
trás de todas as tabelas, ordenada por `trace_id`).

| tabela | base | efeito reportado |
|---|---|---|
| `tool_error` | presença de `tool.name` por trace | `rate_failed` vs `rate_ok`, `rate_difference_pp`, `*_with_error` |
| `model_error` | presença de label de modelo por trace | idem |
| `session_length_error` | tamanho da sessão **contado dentro da janela** (`1` / `2-4` / `5+`) | `failure_rate` por bucket, delta vs janela |
| `retry_eval` | `retry_signals` = invocações repetidas mesmo-nome + retry counters declarados (buckets `0` / `1-2` / `3+`) | `mean_score` observado por bucket, delta vs janela |

Pisos honestos: comparar taxas exige **≥5 traces em cada cohort**
(`failed` e `ok`); bucket de score exige **≥3 traces com score
observado**. Abaixo disso a linha vem com `classification:
"insufficient_data"` + `reason` — os números continuam visíveis
(quando o denominador existe), só a conclusão é recusada. Store vazio
→ contagens zero e rows vazios, nunca erro nem média inventada.

## Limites declarados

- Presença ≠ uso: `tool X aparece em N% dos traces falhos` mede
  co-ocorrência, não culpa.
- Scores de eval só entram quando **observados** em spans
  EVALUATOR — o módulo não roda avaliadores nem inventa score.
- Tamanho de sessão é medido dentro da janela; sessão longa cortada
  pelo bound lê menor (disclosed em `basis`).
- Repetição mesmo-nome é *proxy* de retry (polling/batching
  legítimos têm a mesma cara) — `caveat` carrega isso em cada
  correlação.
- Ordem determinística em tudo: spans por `(start_time, span_id)`,
  rows por `rate_difference` desc + chave, buckets fixos.
