# Referência — MCP (Model Context Protocol)

> Estado: **parcialmente implementado** — o servidor observer stdio
> (`forge-lens mcp-serve`, `src/forge_lens/mcp_server.py`) existe desde o
> ciclo 10 e expõe tools read-only sobre a store local. A instrumentação
> de tráfego MCP descrita abaixo continua **`DOCUMENTED_ONLY`**: nenhuma
> afirmação de suporte deve ser feita além do escopo descrito aqui.

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

## Servidor observer stdio (`forge-lens mcp-serve`)

JSON-RPC 2.0 sobre stdin/stdout (uma mensagem por linha, JSON estrito —
literais `NaN`/`Infinity` são erro de parse), store SQLite aberta em modo
**readonly**. Métodos: `initialize`, `ping`, `tools/list`, `tools/call`.
Cada linha é limitada a **8 MiB** (o mesmo bound do corpo HTTP) — frames
maiores viram `-32700` e o restante é drenado sem re-parsear.

**Lifecycle.** `initialize` negocia `protocolVersion`: a única revisão
suportada é `2024-11-05` — uma string pedida compatível é ecoada,
qualquer outro valor (versão desconhecida, objeto, array, número, nulo)
recebe a suportada de volta (o cliente decide se prossegue); a resposta
anuncia `capabilities: {"tools": {"listChanged": false}}` e
`serverInfo`. **Toda request exige `initialize` antes, exceto `ping`** —
o lifecycle MCP permite ping durante o handshake. Frames **sem `id`**
são notifications e nunca recebem resposta. Um frame
`notifications/*` **com** `id` é um Request malformado (notifications
não carregam `id`) → `-32600`. EOF/disconnect encerra o loop com exit 0 —
uma chamada malformada nunca derruba o servidor.

**Classes de erro.**

| Situação | Resposta |
|---|---|
| linha que não é JSON válido | `-32700` parse error (`id: null`) |
| JSON válido que não é Request (não-objeto; `id` sem `method`) | `-32600` invalid request |
| método desconhecido | `-32601` |
| request pré-`initialize` (exceto `ping`) | `-32602` |
| `params`/`arguments` não-objeto, `name` ausente/mistipado, `TypeError` de validação (tipo errado, `offset` fora de int64, float não-finito, string numérica que não parseia — ex. `"limit": "abc"`) | `-32602` invalid params |
| falha interna da tool | `-32603` **opaco** — `internal tool error: {TipoDaExcecao}`, nunca o texto (pode embutir payload/paths) |
| tool desconhecida ou miss de dados (trace inexistente) | `result` com `isError: true` |

O contrato `isError`: qualquer payload com chave `error` de topo vira
`result.isError` — falha de dados é dado, não falha de protocolo, e o
loop continua servindo.

Tools expostas — espelham a inteligência de leitura da API HTTP.
A matriz completa de parâmetros/limites/erros é gerada do registry vivo
em [`mcp-tools.md`](mcp-tools.md) (`scripts/gen_mcp_matrix.py`; sync
garantido por `tests/unit/test_mcp_matrix.py`):

| Tool | Argumentos | Equivale a |
|---|---|---|
| `lens.list_traces` | `project_id?`, `limit?` (≤500) | `GET /v1/traces` |
| `lens.get_trace` | `trace_id`, `project_id?` | `GET /v1/traces/{id}` |
| `lens.stats` | `project_id?` | stats da store |
| `lens.detect_loops` | `trace_id`, `project_id?` | `GET /v1/traces/{id}/loops` |
| `lens.trace_efficiency` | `trace_id`, `project_id?` | `GET /v1/traces/{id}/efficiency` |
| `lens.correlate` | `trace_id`, `project_id?` | `GET /v1/traces/{id}/correlate` |
| `lens.cost` | `trace_id`, `project_id?` | `GET /v1/traces/{id}/cost` |
| `lens.failure` | `trace_id`, `project_id?` | `GET /v1/traces/{id}/failure` |
| `lens.list_datasets` | `project_id?` | `GET /v1/datasets` |
| `lens.sessions` | `project_id?`, `limit?` (≤500), `offset?` | `GET /v1/sessions` |
| `lens.tools` | `project_id?`, `limit?` (≤500), `offset?` | `GET /v1/tools` |
| `lens.capabilities` | — | `GET /v1/capabilities` |

Notas:

- `lens.cost` usa o mesmo `PricingRegistry` da API —
  `FORGE_LENS_PRICING_FILE`/`config.toml` (`pricing_file`) ou a tabela
  bundled; arquivo malformado falha na subida do servidor (exit 2), nunca
  por chamada.
- Trace inexistente retorna `{"trace_id": ..., "error": "trace not found"}`
  no payload (→ `isError`) — not-found é dado, não falha de protocolo.
  Exceção deliberada: `lens.get_trace` retorna `{"trace": null}` sem
  `isError`, espelhando o 404/200-shape da API.
- `limit`/`offset` são bounded: `limit` clampa em `[0, 500]`; `offset`
  fora do range int64 do SQLite é `-32602`, não um erro de driver.
