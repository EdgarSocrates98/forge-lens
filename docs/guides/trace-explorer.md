# Guia — Trace Explorer

> Estado atual: **CLI + API** (ciclos 01–06). O Studio visual é ciclo 02 —
> `NOT_STARTED`, ver `docs/cycles/cycle-02-final-report.md`. Status dos
> itens marcados *UI*: `DOCUMENTED_ONLY`.

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

## Roadmap visual (ciclo 02 Studio)

Timeline waterfall, filtro por kind/atributo, painel de evidências,
comparação lado-a-lado de execuções — tudo sobre os mesmos contratos v1 e
as APIs já implementadas (`/v1/graph/*`, `/v1/traces/{id}/replay`).
