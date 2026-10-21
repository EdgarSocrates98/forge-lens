# Guia — Importar traces

## De arquivo (offline)

```bash
forge-lens ingest otlp-export.json --format auto --project investigacao
```

`auto` detecta OTLP pela presença de `resourceSpans`; `--format native`
força o envelope `TraceIngest`. O resultado mostra spans aceitos,
duplicatas e warnings de integridade.

## De um exportador OTel existente

Qualquer SDK/agente que emita OTLP/HTTP **JSON** aponta para
`POST http://127.0.0.1:7777/v1/otlp/traces`. OpenInference (`openinference.*`)
e GenAI semconv (`gen_ai.usage.*`) são mapeados automaticamente.

> Protobuf OTLP não é aceito nesta versão — exporte JSON ou converta.

## Re-ingestão é segura

Dedup por `(project_id, trace_id, span_id)`: reimportar o mesmo arquivo
reporta `duplicate_spans` e não corrompe o store.

## Validar antes de confiar

```bash
forge-lens traces show <trace_id>    # flags de integridade ~ missing_parent etc.
```

Traces parciais são aceitas e marcadas — investigue `warnings` do ingest
antes de tratar o grafo como completo.
