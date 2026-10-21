# Guia — Trace Explorer

> Estado atual: **CLI + API** (ciclo 01). O Studio visual é ciclo 03 —
> descrito em roadmap. Status dos itens marcados *UI*: `DOCUMENTED_ONLY`.

## CLI (implementado)

```bash
forge-lens traces list --status error --limit 50
forge-lens traces show <trace_id>
forge-lens traces show <trace_id> --json
```

A árvore mostra: `!` erro, duração, `~ flag:` integridade
(`missing_parent`, `clock_skew`, `truncated`…), `~ tokens:` por span LLM.

## API (implementado)

`GET /v1/traces` (filtros project/session/status) e
`GET /v1/traces/{id}` retornam o contrato completo — base para qualquer UI.

## Lendo uma investigação

1. Liste por `status=error` → escolha a trace.
2. Na árvore, encontre o span `!` — erro propaga para o status da trace.
3. Verifique flags de integridade **antes** de concluir causalidade:
   `missing_parent` pode significar trace parcial, não bug.
4. `forge_lens.token_usage` em spans `llm` dá o custo em tokens medido.

## Roadmap visual (ciclo 03)

Timeline waterfall, filtro por kind/atributo, painel de evidências,
comparação lado-a-lado de execuções — tudo sobre os mesmos contratos v1.
