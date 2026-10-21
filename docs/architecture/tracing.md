# Arquitetura — Modelo de Tracing

## Conceitos

- **Trace**: árvore de trabalho de uma execução de agente (possivelmente
  parcial). Identidade: W3C `trace_id` (32 hex).
- **Span**: unidade de trabalho (16 hex). `kind` une semântica OTel
  (`internal|server|client|producer|consumer`) e OpenInference
  (`agent|chain|llm|tool|retriever|embedding|reranker|guardrail|evaluator`).
- **Event**: ponto no tempo dentro de um span (ex.: `exception`).
- **Link**: aresta para outra trace/span (delegação entre processos).

## Propagação de contexto

- Python SDK: `contextvars` (stack local) + `SpanContext` com
  `traceparent` W3C para fronteiras de processo.
- TS SDK: `AsyncLocalStorage` com a mesma semântica.
- `remote_parent` permite continuar uma trace recebida via headers — sem
  isso, um span sem parent é raiz de nova trace.

## Semântica de tempo

- `start_time`/`end_time` em `datetime` UTC tz-aware (ISO-8601 no fio).
- SQLite persiste `started_ns`/`ended_ns` como inteiro unix-nano
  (ordenação exata, mesma convenção OTLP).
- `duration_ms` é *derivada* (nunca armazenada como fato primário no contrato).

## Status

`unset | ok | error` por span; status da trace = rollup
`error > ok > unset`. Exceções no SDK viram `status=error` + evento
`exception` com `exception.type`/`exception.message` — o erro propaga, o
span fecha, a trace flui.

## Integridade não-fatal

Spans imperfeitos são *aceitos e marcados* (`integrity_flags`) — nunca
silenciosamente corrigidos nem rejeitados por órfão/parent ausente.
