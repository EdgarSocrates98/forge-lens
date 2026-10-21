# Referência — MCP (Model Context Protocol)

> Estado: **`DOCUMENTED_ONLY`** — roadmap ciclo 09. Nenhum código MCP existe
> no ciclo 01 e nenhuma afirmação de suporte deve ser feita além do escopo
> descrito aqui.

## Escopo honesto

Forge Lens **não** intercepta tráfego MCP universalmente — não existe hook
mágico fora do cliente/servidor. A instrumentação MCP só é válida onde
existir um **ponto de instrumentação real**:

1. **Cliente instrumentado**: o host que invoca `tools/call` embrulha a
   chamada num span `kind=tool` com `mcp.server`, `mcp.tool.name`,
   `mcp.request_id` — via SDK Lens ou middleware do cliente.
2. **Servidor instrumentado**: o server MCP emite spans próprios (via SDK
   Lens dentro do handler) com `remote_parent` extraído de metadados do
   request quando o protocolo permitir propagação.
3. **Gateway/proxy**: um proxy MCP consciente pode enriquecer mensagens
   com `traceparent` e emitir spans de fronteira.

## Mapeamento proposto (draft v1)

| MCP | Forge Lens |
|---|---|
| `tools/call` | span `kind=tool`, `tool.name`, `mcp.server` |
| `tools/list`, `resources/*`, `prompts/*` | span `kind=internal` com `mcp.method` |
| request/response id | `mcp.request_id` atributo |
| erro JSON-RPC | `status=error` + `exception.*` |
| progress/notifications | `SpanEvent` |

## Limites declarados

- Sem propagação de contexto quando o transporte/peer não suporta metadados —
  nesse caso o span vira raiz de trace própria com `provenance=observed` e
  nenhuma pretensão de correlação.
- Nenhum claim de captura de stdio/SSE sem instrumentação explícita.
