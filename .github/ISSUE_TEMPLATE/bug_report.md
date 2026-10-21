---
name: Bug report
about: Reporte um comportamento incorreto do Forge Lens
title: "[bug] "
labels: [bug]
assignees: []
---

<!-- Obrigado pelo report. Claims aqui seguem a mesma regra do projeto:
     evidência primeiro — cole saídas reais, não paráfrases. -->

## O que aconteceu

<!-- Descreva o comportamento observado. -->

## Como reproduzir

<!-- Passos mínimos, comandos exatos ou trecho de código. -->

```bash
# ex.: forge-lens traces list --db lens.db
```

## O que era esperado

## Evidência

<!-- Saída real do comando, traceback, trace_id relevante.
     NÃO cole segredos: use `capture=metadata` e verifique a saída antes
     de colar — o sanitize do Lens não roda no seu clipboard. -->

```text
(cole aqui)
```

## Ambiente

| Item | Valor |
|---|---|
| OS | <!-- ex.: Ubuntu 24.04 / macOS 15 / Windows 11 --> |
| Python | <!-- `python --version` --> |
| forge-lens | <!-- `forge-lens --version` ou SHA do clone --> |
| Instalação | <!-- uv / pip / repo clone --> |
| Modo | <!-- CLI local / `forge-lens serve` / importador --> |

## Contexto extra

<!-- DB existente? auth_mode? Algo relevante do `.forge-lens/config.toml`
     (sem segredos)? -->

> Vulnerabilidade de segurança? **Não abra issue pública** — use
> [GitHub Security Advisories](../../security/advisories) conforme
> `SECURITY.md`.
