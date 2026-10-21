# ADR-0003 — Sanitização pré-persistência com capture levels

**Status**: ACCEPTED (ciclo 00)

## Contexto

Traces de agentes carregam segredos (API keys em env/headers), PII e corpos
potencialmente enormes. Sanitizar "na leitura" deixa o dano persistido;
sanitizar no produtor é frágil (não controlamos o OTLP de terceiros).

## Decisão

Sanitização obrigatória **dentro do pipeline, antes do store**, com dois
níveis: `metadata` (default — redige segredos, descarta corpos de
prompt/tool marcando `_forge_lens.dropped`, mascara e-mails) e `full`
(opt-in explícito). Toda alteração é contada em `SanitizeReport` +
métricas — nada é removido silenciosamente.

## Consequências

- Dado sensível nunca toca o disco no default — o risco de vazamento é de
  memória, não de persistência.
- `full` é escolha consciente do operador (config/env), não default.
- Chaves de *métricas* com "token" (token_count, usage.*tokens) são
  explicitamente preservadas — redação errada destruiria telemetria
  (regressão coberta em teste).

## Alternativas rejeitadas

- **Sanitizar só no SDK**: exportadores OTLP de terceiros passariam dados
  brutos ao store.
- **Sanitizar na leitura**: segredos continuariam em `lens.db` e backups.
- **Hash em vez de drop**: hash de prompt revela conteúdo previsível —
  drop é mais honesto no default.
