# Guia — Trace Explorer

> Estado atual: **CLI + API + Studio** — o Trace Explorer visual está
> implementado em `apps/studio` (Angular) e coberto pela suíte E2E
> Playwright (`docs/studio-e2e.md`).

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

## Studio (implementado)

- `/traces` — lista com filtros por status, busca textual
  (trace id / span name), ferramenta, sessão, modelo, agente, host,
  projeto, `min_total_tokens`, faixa de duração e janela temporal;
  exportação JSON da listagem.
- `/traces/:id` — detalhe com abas **waterfall** (barras por span com
  indentação de profundidade e erro destacado) e **evidence**, mais
  painéis de custo, falha, loops, análise de grafo, **replay** com
  play/pause/step e **eval** (rodar avaliadores selecionados com
  parâmetros).

## Lendo uma investigação

1. Liste por `status=error` → escolha a trace.
2. Na árvore, encontre o span `!` — erro propaga para o status da trace.
3. Verifique flags de integridade **antes** de concluir causalidade:
   `missing_parent` pode significar trace parcial, não bug.
4. `forge_lens.token_usage` em spans `llm` dá o custo em tokens medido.

## Ainda fora da UI

Incident bundle e stream ao vivo (`/v1/traces/{id}/incident`,
`…/stream`) e comparação lado-a-lado de traces arbitrárias seguem
API/CLI-only — a suíte `/experiments` cobre comparação de cohorts, não
de traces avulsas.
