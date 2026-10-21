# Arquitetura — Segurança

## Safe-by-default

| Controle | Default | Como mudar |
|---|---|---|
| Capture level | `metadata` | `capture="full"` em config/env (opt-in) |
| Bind | `127.0.0.1` | `--host` (decisão do operador) |
| Auth ingestão | desligada (local) | `api_key` → `X-Forge-Lens-Key` obrigatório |
| Auth leitura | aberta | modelo local single-user; proteger via rede |

## Pipeline de sanitização (pré-persistência, sempre ativa)

1. **Chaves sensíveis** (api_key, secret, *token* de acesso/refresh/id/
   session/auth/api/bearer/csrf, password, authorization, cookie, PEM, …)
   → `[REDACTED:key]`. *Contadores* (`llm.token_count.*`, `*.usage.*tokens`)
   não são redigidos — são métricas, não segredos.
2. **Padrões em valores** (sk-ant-*, sk-*, AIza*, AKIA*, ghp_*, github_pat_*,
   xox*, blocos PEM, `Bearer x`, JWT) → `[REDACTED:…]` inline.
3. **`capture=metadata`**: atributos de conteúdo (`input.value`,
   `output.value`, `llm.*_messages`, `tool.parameters`,
   `retrieval.documents`) → `{"_forge_lens.dropped": true, _approx_bytes}`;
   e-mails → `[MASKED:email]`.
4. **Truncamento** > `max_attr_value_chars` com marcador explícito.
5. Relatório `SanitizeReport` por ingestão + métricas `lens.sanitize.*`.

## Limites e abuso

- `max_payload_bytes`, `max_spans_per_request` → 413.
- `max_events_per_span` → trunca com flag `TRUNCATED`.
- `rate_limit_per_minute` (off por default local) → 429.
- OTLP protobuf não aceito — superfície de parsing reduzida a JSON.

## O que está fora do escopo (honesto)

- TLS: não embutido — atrás de proxy/local apenas (roadmap: ciclo deploy).
- RBAC/tenancy forte: `project_id` particiona dados mas não é fronteira de
  segurança; multi-tenant real é ciclo posterior.
- Audit log dedicado: ciclo de governança; hoje há métricas de ingestão.
- `file:`/URIs remotos em `BlobRef`: referências são *dados*, nunca
  resolvidas pelo servidor (sem SSRF por design).

Ver `docs/security/threat-model.md` para análise completa.
