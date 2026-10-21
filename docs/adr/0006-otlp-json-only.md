# ADR-0006 — OTLP aceita só JSON nesta versão

**Status**: ACCEPTED (ciclo 01)

## Contexto

OTLP/HTTP tem dois codecs: JSON e protobuf. Suportar protobuf exige
dependência + schemas do collector + outra superfície de parsing.

## Decisão

`POST /v1/otlp/traces` aceita **somente** `ExportTraceServiceRequest` em
JSON; protobuf não é detectado nem convertido — o endpoint documenta isso
e a ingestão de arquivo igual. Parser tolerante a variações do JSON
(unixNano string/int, `AnyValue`, seções ausentes).

## Consequências

- Menos deps e menos superfície de ataque/parsing.
- Quem emite protobuf precisa converter para JSON antes — declarado no doc
  e no endpoint (não fingimos suporte).
- Reversível: adicionar protobuf depois é aditivo, não breaking.

## Alternativas rejeitadas

- **Aceitar "e ver o que passa"**: silenciosamente engolir binário como JSON
  quebrado produziria erros enganosos.
- **Contar com protobuf desde já**: atrasa a fundação sem ganho no ciclo 01.
