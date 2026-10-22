# Arquitetura — Segurança

## Safe-by-default

| Controle | Default | Como mudar |
|---|---|---|
| Capture level | `metadata` | `capture="full"` em config/env (opt-in) |
| Bind | `127.0.0.1` | `--host` (decisão do operador) |
| Auth ingestão | desligada (local) | `api_key`/`auth_mode` → `X-Forge-Lens-Key` obrigatório |
| Auth leitura | aberta | `auth_mode=keys` fecha tudo com papéis + escopo; `auth_reads=true` estende `api_key` a leituras |

## Pipeline de sanitização (pré-persistência, sempre ativa)

1. **Chaves sensíveis** (api_key, secret, *token* — snake e camelCase:
   `access_token`, `accessToken`, `refreshToken`, `idToken`, …, password,
   authorization, cookie, PEM, …) → `[REDACTED:key]`. *Contadores*
   (`llm.token_count.*`, `*.usage.*tokens`) não são redigidos — são
   métricas, não segredos (terminam em `count`/`usage`/`tokens`).
2. **Padrões em valores** (sk-ant-*, sk-*, AIza*, AKIA*, ghp_*, github_pat_*,
   glpat-*, npm_*, pypi-*, hf_*, xox*, blocos PEM, `Bearer x`, JWT,
   `Authorization: <scheme> <cred>`, userinfo `scheme://user:pass@host`,
   e formas `password=`/`token:`/`api_key=`/`x-api-key:` em texto livre)
   → `[REDACTED:…]` inline — em atributos *e* em campos de texto livre
   do contrato: `status.message`
   (span+trace — carrega texto de exceção), `span.name`, `event.name`,
   `source.*` (`system`, `sdk_name`, `sdk_version`, `host`, `runtime`,
   `language`), `session_id` e `project_id` (esses dois sem e-mail-mask:
   são chaves de lookup/partição — mascarar quebraria o join). IDs de
   join (`trace_id`, `span_id`) nunca são tocados.
3. **`capture=metadata`**: atributos de conteúdo (`input.value`,
   `output.value`, `llm.*_messages`, `tool.parameters`,
   `retrieval.documents`) → `{"_forge_lens.dropped": true, _approx_bytes}`;
   e-mails → `[MASKED:email]`.
4. **Truncamento** > `max_attr_value_chars` com marcador explícito.
5. Relatório `SanitizeReport` por ingestão + métricas `lens.sanitize.*`.

## Limites e abuso

- `max_payload_bytes`, `max_spans_per_request`, `max_traces_per_request`
  → 413.
- `max_events_per_span` → trunca com flag `TRUNCATED`.
- `rate_limit_per_minute` (off por default local) → 429.
- OTLP protobuf → 415 explícito — superfície de parsing = JSON apenas.

## O que está fora do escopo (honesto)

- TLS: o `serve` aceita `--ssl-certfile`/`--ssl-keyfile` (passthrough do
  uvicorn, ciclo 08); terminação dedicada fica a cargo de proxy
  quando a exposição exige mais do que isso.
- RBAC/tenancy forte: ciclo 08 entregou API keys (`X-Forge-Lens-Key`),
  papéis `reader|writer|admin` e `project_id` como fronteira de escopo —
  `project_id` particiona dados e agora bloqueia acesso cruzado por key
  scoping; SSO/OIDC empresarial permanece fora do escopo local-first.
- Audit log: existe (`list_audit`, ciclo 08) cobrindo ações administrativas;
  cobertura forense completa (p.ex. leituras) é resíduo documentado.
- `file:`/URIs remotos em `BlobRef`: referências são *dados*, nunca
  resolvidas pelo servidor (sem SSRF por design).

Ver `docs/security/threat-model.md` para análise completa.
