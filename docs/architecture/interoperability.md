# Arquitetura — Interoperabilidade

## Padrões adotados

| Padrão | Uso | Estado |
|---|---|---|
| W3C Trace Context | ids + `traceparent` propagação | implementado (Py+TS) |
| OTLP/HTTP JSON | `POST /v1/otlp/traces` | implementado |
| OTLP protobuf | — | rejeitado explicitamente (backlog) |
| OpenInference attrs | `openinference.span.kind`, `llm.*` | mapeado |
| OTel GenAI semconv | `gen_ai.usage.*`, `gen_ai.system`, `session.id` | mapeado (parcial, tolerante) |
| JSON Schema | `schemas/v1/*.json` para consumidores externos | gerado e commitado |

## Pontos de extensão

- **`TraceStore` protocol**: backends alternativos sem mudar pipeline.
- **Exporter duck-type** no SDK: qualquer objeto `export(traces, source)`.
- **Contratos `extra="allow"`**: produtores novos não quebram consumidores.
- **Ingest de arquivo**: `forge-lens ingest` aceita qualquer JSON que valide
  contra `TraceIngest` ou OTLP — porta de importação de traces de outras
  ferramentas (Claude Code, agentes próprios) via adaptadores futuros.

## Integrações Forge

- Sem dependência de código dos repos Forge; integração = **artefatos**:
  export de `Trace`/`Evidence`/`EvaluationResult` em contrato v1 +
  `forge-lens ingest`.
- MCP: instrumentação só onde existir ponto real de instrumentação
  (cliente/servidor/gateway) — nunca claim de interceptação universal
  (ver `docs/reference/mcp.md`).

### the-forge → forge-lens (verificado e2e)

`theforge telemetry export` re-deriva um `ExportTraceServiceRequest` OTLP
dos artefatos `RunTelemetry/v1` persistidos (`.forge/runs/<id>/telemetry.json`)
mais os recibos de `.forge/orchestrations/`:

```bash
theforge telemetry export --correlation <corr> --out otlp.json
forge-lens ingest otlp.json
forge-lens traces list --session <corr>   # session.id carrega o correlation_id
```

O documento é determinístico (`traceId`/`spanId` são sha256 de ids estáveis:
correlation id, run id, span id — re-exportar produz bytes idênticos,
re-ingerir deduplica por `(project, trace, span)`). A provenance do ingest de
arquivo é `imported` — o estado honesto para evidência re-derivada, nunca
promovida a `observed`. Estrutura: recibo de orquestração vira a raiz
`theforge.orchestrate`; cada run vira `theforge.<kind>`; spans locais do
`RunTelemetry` aninham com `theforge.run_id` por atributo. `parent_run` fora
da exportação marca `MISSING_PARENT` até o pai ser ingerido — nunca id
inventado.

## Não-objetivos de interop

- Não emulamos a API do Langfuse/Phoenix (import por arquivo cobre migração).
- Não falamos protobuf OTLP ainda — declarado, não escondido.
