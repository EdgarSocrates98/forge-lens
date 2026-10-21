# Cycle 05 — Relatório Final: Investigação, Replay e Bundles de Incidente

**Status**: `PARTIAL` — backend determinístico implementado (classificação
de falha, replay de evidência registrada, bundle de incidente); root-cause
hipotético multi-hipótese e a UI de investigação dependem de Cycles 02/07.
**Data**: 2026-10-21

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| `classify_failure` — 7 categorias ordenadas por especificidade, com evidência | `src/forge_lens/investigate/classify.py` | VERIFIED |
| `replay_timeline` — timeline único intercalando span_start/event/span_end | `src/forge_lens/investigate/replay.py` | VERIFIED |
| `incident_bundle` — composição failure+graph+cost+waste+replay | `src/forge_lens/investigate/bundle.py` | VERIFIED |
| Endpoints `/failure`, `/replay`, `/incident` | `src/forge_lens/api/app.py` | VERIFIED |
| CLI `forge-lens incident <trace_id>` | `src/forge_lens/cli/main.py` | VERIFIED |
| Testes unitários + API | `tests/unit/test_graph_intel.py`, `tests/integration/test_api.py` | VERIFIED |

## 2. Regras determinísticas (classificação)

Ordem de especificidade — a primeira regra que casa vence, e toda
classificação nomeia spans/atributos em `evidence`:

1. `cycle` — span declara a si mesmo como pai (topologia corrompida).
2. `rate_limit` — `http.status_code=429` ou "rate limit" em mensagem.
3. `timeout` — regex `timeout|timed out|deadline|exceeded` em
   status.message/`error.message`/`exception.message`/`http.status_text`.
4. `guardrail` → `tool_error` → `llm_error` — erro em span do kind.
5. `incomplete` — spans com `missing_end` e nenhum span de erro
   (`confidence: inferred` — host provavelmente morreu no meio do run).
6. `unknown` — resposta válida e honesta; `failed=false` em traces
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

## 5. O que NÃO está neste ciclo (honesto)

- **Root-cause multi-hipótese ranqueada** — hoje uma categoria por trace;
  ranking de hipóteses concorrentes é Cycle 07 (specialist agents).
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
