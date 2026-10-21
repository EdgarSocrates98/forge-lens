# Guia — Quickstart

```bash
# 1. instalar (do repositório)
uv sync --extra server
uv run forge-lens doctor

# 2. inicializar um projeto
cd meu-projeto
forge-lens init . --project meu-projeto

# 3. subir a API local
forge-lens serve            # http://127.0.0.1:7777
```

## Primeiro trace em 10 linhas

```python
from forge_lens.sdk import LensTracer
from forge_lens.sdk.exporter import HTTPExporter

tracer = LensTracer(
    service_name="demo",
    exporter=HTTPExporter("http://127.0.0.1:7777", project_id="meu-projeto"),
)

with tracer.agent("demo-agent"):
    with tracer.llm_call("claude-sonnet-4", provider="anthropic") as s:
        tracer.record_usage(s, input_tokens=120, output_tokens=40)

# auto_flush envia ao fechar o span raiz; tracer.flush() força manual
```

## Inspecionar

```bash
forge-lens traces list
forge-lens traces show <trace_id>
curl http://127.0.0.1:7777/v1/health
curl http://127.0.0.1:7777/v1/traces
```

## Ingerir um arquivo OTLP existente

```bash
forge-lens ingest export-otlp.json --format auto
```

Próximos passos: `docs/guides/instrument-agent.md`,
`docs/guides/import-traces.md`, `docs/guides/trace-explorer.md`.
