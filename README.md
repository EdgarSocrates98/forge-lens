# Forge Lens

**Local-first, open-source agentic observability.** Understand every agent execution. Trace every tool. Measure every cost. Investigate every failure. Prove every improvement.

Forge Lens receives, persists and explains agent executions — via OTLP/HTTP, OpenInference attributes, or its own SDKs — on your machine, with no mandatory external dependency.

## Status

Cycle 01 (foundation): contracts v1, OTLP/HTTP JSON ingestion, sanitization, SQLite WAL store, Python SDK, CLI. See `docs/cycles/` for reports.

## Quickstart

```bash
uv sync --extra server
forge-lens init .
forge-lens serve            # API on http://127.0.0.1:7777
forge-lens doctor
```

Instrument an agent:

```python
from forge_lens.sdk import LensTracer
from forge_lens.sdk.exporter import HTTPExporter

tracer = LensTracer(
    service_name="my-agent",
    exporter=HTTPExporter("http://127.0.0.1:7777"),
)

with tracer.agent("researcher"):
    with tracer.llm_call("claude-sonnet-4", provider="anthropic") as s:
        tracer.record_usage(s, input_tokens=200, output_tokens=80, total_tokens=280)
    with tracer.tool_call("web_search"):
        ...  # call the tool
```

Inspect:

```bash
forge-lens traces list
forge-lens traces show <trace-id>
```

## Principles

- **Local-first** — SQLite WAL by default; works fully offline.
- **Evidence-first** — every claim carries provenance (`observed` / `imported` / `derived` / `inferred` / `unknown`). Inferred never silently becomes observed.
- **Safe by default** — `capture=metadata` redacts secrets and drops prompt bodies unless you opt into `full`.
- **OTel/OpenInference compatible** — OTLP/HTTP JSON ingest, `openinference.*` attribute mapping, W3C traceparent propagation.

## Documentation

See `docs/` — architecture, guides, API/CLI/contracts reference, ADRs and cycle reports (PT-BR first, EN planned).
