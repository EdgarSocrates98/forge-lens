# Referência — tools MCP (matriz gerada)

<!-- GERADO por scripts/gen_mcp_matrix.py a partir do registry
     real (forge_lens.mcp_server._tools) — não editar à mão.
     Regenere: .venv/bin/python scripts/gen_mcp_matrix.py -->

O servidor `forge-lens mcp-serve` expõe **12 tools**
read-only sobre a store local. `*` = parâmetro obrigatório,
`?` = opcional.

| Tool | Descrição | Parâmetros | Limites | Erros de dados |
|---|---|---|---|---|
| `lens.capabilities` | Host adapter capability matrix — honest coverage claims. | — | — | — |
| `lens.correlate` | Temporal co-occurrence signals within a trace (retries→errors, model switches, context growth). Descriptive associations, never causal claims. | `project_id?` (string)<br>`trace_id*` (string) | `trace_id` string não-vazia | trace ausente → `isError` (`trace not found`) |
| `lens.cost` | Derived cost report for one trace (registry-priced; includes waste findings). | `project_id?` (string)<br>`trace_id*` (string) | `trace_id` string não-vazia | trace ausente → `isError` (`trace not found`) |
| `lens.detect_loops` | Heuristic loop/no-progress findings with span evidence. | `project_id?` (string)<br>`trace_id*` (string) | `trace_id` string não-vazia | trace ausente → `isError` (`trace not found`) |
| `lens.failure` | Deterministic failure classification with span evidence. | `project_id?` (string)<br>`trace_id*` (string) | `trace_id` string não-vazia | trace ausente → `isError` (`trace not found`) |
| `lens.get_trace` | Fetch one trace by id. | `project_id?` (string)<br>`trace_id*` (string) | `trace_id` string não-vazia | trace ausente → `{"trace": null}` |
| `lens.list_datasets` | List eval datasets in the store. | `project_id?` (string) | — | — |
| `lens.list_traces` | List recent traces (bounded, sorted). | `limit? =20` (integer)<br>`project_id?` (string) | `limit` int em [0, 500] | — |
| `lens.sessions` | Session roll-up — one row per session_id, newest activity first (matches /v1/sessions). | `limit? =100` (integer)<br>`offset? =0` (integer)<br>`project_id?` (string) | `limit` int em [0, 500]; `offset` int em [0, 2^63-1] | — |
| `lens.stats` | Store statistics snapshot. | `project_id?` (string) | — | — |
| `lens.tools` | Fleet-wide tool roll-up — calls/traces/errors/avg duration per tool.name (matches /v1/tools). | `limit? =100` (integer)<br>`offset? =0` (integer)<br>`project_id?` (string) | `limit` int em [0, 500]; `offset` int em [0, 2^63-1] | — |
| `lens.trace_efficiency` | Per-trace efficiency roll-up (tokens/tools/retries). | `project_id?` (string)<br>`trace_id*` (string) | `trace_id` string não-vazia | trace ausente → `isError` (`trace not found`) |

Classes de erro uniformes (protocolo, todas as tools):

- argumento malformado (validação `TypeError`) → `-32602`
  `invalid params`;
- tool name ausente/mistipado → `-32602`; tool name desconhecido
  → `result.isError` (miss de dados, não falha de protocolo);
- falha interna → `-32603` opaco (`internal tool error:
  {TipoDaExcecao}` — nunca o texto da exceção);
- frame inválido → `-32700` (parse) / `-32600` (request) /
  `-32601` (método); request pré-`initialize` → `-32602`.

Contrato `isError`: qualquer payload com chave `error` de topo é
retornado como `result.isError = true` — o loop nunca morre por
dado faltante.
