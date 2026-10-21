# Cycle 05 — Relatório Final: Investigação, Replay e Bundles de Incidente

**Status**: `PARTIAL` — backend determinístico implementado (classificação
de falha, replay de evidência registrada, bundle de incidente); root-cause
hipotético multi-hipótese e a UI de investigação dependem de Cycles 02/07.
**Data**: 2026-10-21

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| `classify_failure` — taxonomia §05.3 completa (15 categorias) ordenadas por especificidade, com evidência | `src/forge_lens/investigate/classify.py` | VERIFIED |
| `replay_timeline` — timeline único intercalando span_start/event/span_end | `src/forge_lens/investigate/replay.py` | VERIFIED |
| `incident_bundle` — composição failure+graph+cost+waste+replay | `src/forge_lens/investigate/bundle.py` | VERIFIED |
| Endpoints `/failure`, `/replay`, `/incident` | `src/forge_lens/api/app.py` | VERIFIED |
| CLI `forge-lens incident <trace_id>` | `src/forge_lens/cli/main.py` | VERIFIED |
| Testes unitários + API | `tests/unit/test_graph_intel.py`, `tests/integration/test_api.py` | VERIFIED |

## 2. Regras determinísticas (classificação)

Ordem de especificidade — a primeira regra que casa vence, e toda
classificação nomeia spans/atributos/sinais em `evidence`. Taxonomia
completa §05.3 (wave pós-review — ver
`docs/guides/failure-investigation.md` para a tabela de sinais):

1. `cycle` — qualquer membro de um ciclo real de parentesco na
   topologia armazenada (self-loop ou multi-nó; mesmo walk O(n) do
   graph builder). `failed=true` — a topologia declarada é impossível.
2. `budget_exceeded` — flags `*.budget.exceeded` ou termos quota/budget
   (429 nomeando quota ganha de `rate_limit`).
3. `auth_error` — 401 / `*Auth*`/`Unauthorized` exception types /
   credencial inválida ou expirada.
4. `missing_permissions` — 403 / `PermissionError` / forbidden /
   permission denied.
5. `schema_mismatch` — schema/structured-output/parse evidence (mais
   específico que um 400 nu).
6. `invalid_arguments` — 400/422 / `ValidationError` family /
   invalid-missing-argument text.
7. `retry_exhaustion` — counters `retry.*`/`attempt*` ≥ max registrado
   ou ≥3, flags, ≥3 `retry` events, `RetryError`.
8. `rate_limit` — `http.status_code=429` puro ou "rate limit" em
   mensagem **de um span que errou** (429 recuperado não é causa).
9. `timeout` — regex `timeout|timed out|deadline` (sem "exceeded" nu:
   "context length exceeded" não é timeout) em
   status.message/`error.message`/`exception.message`/`exception.type`/
   `http.status_text` e agora também em exception events.
10. `agent_cancelled` — `CancelledError`/flags `cancelled` (forte, em
    qualquer span) ou status-message cancel (fraco, só em erro);
    cancelamento marca `failed=true`.
11. `guardrail` → `tool_error` → `llm_error` — erro em span do kind.
12. `incomplete` — spans com `missing_end` e nenhum span de erro
    (`confidence: inferred` — host provavelmente morreu no meio do run).
13. `unknown` — resposta válida e honesta; `failed=false` em traces
    saudáveis.

`confidence: observed` quando há evidência direta de span;
`inferred` quando a categoria é uma hipótese estrutural.

## 3. Replay — escopo honesto

`replay_timeline` **re-percorre evidência registrada** — não re-executa
código do agente (o processo host não existe no lado do Lens). A timeline
é determinística (`(timestamp, span_id, kind)`), com `offset_ms` relativo
ao primeiro timestamp para comparabilidade entre máquinas, e
`unclosed_spans` contabiliza spans sem `end_time`. Re-execução real de
agente permanece fora do escopo local-first.

## 4. Incident bundle

`bundle_version: "1.0.0"` — um payload JSON compondo: `failure` (com
evidence) + `graph` (stats incluindo `cycle_members`, critical path
iterativo, top-5 self-time) + `cost` (provenance derived) + `waste` +
`replay`. Projetado para handoff/export — um `curl` produz o pacote
completo do incidente.

## 5. O que NÃO estava neste ciclo (honesto — status atualizado)

- **Root-cause multi-hipótese ranqueada** — era uma categoria por trace;
  o modelo de hipóteses §05.4 (`build_hypotheses` — primária +
  concorrentes, com supporting/contradictory/missing evidence e status
  honesto) landed na wave pós-review — ver
  `docs/cycles/cycle-13-improvement-wave.md` e
  `docs/guides/failure-investigation.md`.
- **Replay comparativo** (before/after entre duas execuções) — requer
  pares de traces relacionados; previsto com experimentos (Cycle 06).
- **Incident timeline UI** — Cycle 02.
- **Alertas automáticos** sobre classificações — Cycle 07.

## 6. Evidência

- `test_classify_timeout_and_tool_error`,
  `test_classify_rate_limit_and_llm`,
  `test_classify_incomplete_and_unknown` — regras exercitadas por categoria.
- `test_replay_timeline_orders_and_offsets` — ordenação, offsets,
  intercalação de eventos.
- `test_incident_bundle_composes_evidence`,
  `test_failure_replay_incident_endpoints` — composição via API real
  (synthetic trace com tool error → `category=timeout`, replay com
  `span_start`/`event`/`span_end`, bundle com todas as seções).
- `forge-lens incident <id>` imprime categoria + evidência + path +
  timeline + custo.
