# Guia — Integrações

## OTel/OpenInference (implementado)

Aponte exportadores OTLP/HTTP **JSON** para `POST /v1/otlp/traces`.
Atributos `openinference.*`, `gen_ai.*`, `llm.*`, `session.id`,
`telemetry.sdk.language` são mapeados; o resto é preservado em attributes.

## SDKs Lens (implementado)

- Python: `forge_lens.sdk` (ver `docs/reference/sdk-python.md`).
- TypeScript: `@forge-lens/sdk` zero-dep (ver `docs/reference/sdk-typescript.md`).

## Ecossistema Forge (preparação — ciclo 10)

Integração é **por artefato**, nunca por dependência de código:

1. `forge-lens` exporta `Trace`/`Evidence`/`EvaluationResult` (contratos v1).
2. Ferramentas Forge consomem via `forge-lens ingest` ou API local.
3. O Lens **não exige** The Forge, Forge CLI, Forge Portal, Data Sentinel,
   LLMs pagos, AWS, Docker, K8s ou ClickHouse — funciona standalone.

## MCP (roadmap ciclo 09)

Ver `docs/reference/mcp.md` — escopo declarado: só onde existir ponto real
de instrumentação (cliente/server/gateway).

## Importadores de outras ferramentas

Via arquivo: qualquer JSON que valide `TraceIngest` ou OTLP JSON entra por
`forge-lens ingest`. Adaptadores dedicados (Claude Code export, etc.) são
backlog — o formato do adaptador é sempre o contrato v1.
