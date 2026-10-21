# Segurança — Threat Model

## Ativos

- Conteúdo de execuções de agente (prompts, args de tool, outputs, tokens).
- Integridade do store (dedupe/provenance corretos).
- Disponibilidade do processo local.

## Atores e superfícies

| Superfície | Ameaça | Mitigação atual |
|---|---|---|
| `POST /v1/traces`, `/v1/otlp/traces` | injeção de payloads malformados | validação Pydantic + parser tolerante + limites (413) |
| mesma | flood/DoS | rate limit opcional + caps de payload/traces/spans |
| mesma | ingest não autorizado (porta exposta) | `api_key` + bind `127.0.0.1` default + aviso no `serve` quando o bind não é loopback sem `api_key` |
| atributos de spans | segredos em valores | redaction de chaves + padrões em valores |
| corpos de prompt/tool | PII/segredos persistidos | `capture=metadata` descarta por default |
| leitura `/v1/traces*` | vazamento de traces a terceiros locais | sem auth por design local — exponha só em rede confiável |
| `BlobRef` | SSRF por resolução de URI | referências nunca resolvidas pelo servidor |
| SDK→API | MITM localhost | HTTP local; TLS fica a cargo do operador em exposição real |
| dependências | supply chain | deps mínimas + `pip-audit` no CI + dependabot |
| store | corrupção/injeção SQL | parâmetros sempre bindados; SQL estático |
| processo | exfiltração pelo produto | sem telemetria/saída externa por design |

## Decisões assumidas (declaradas)

- **Leitura aberta** no modelo local single-user — não é fronteira de auth;
  expondo a porta, a proteção é de rede/proxy.
- **OTLP protobuf recusado** — reduz parser e superfície de ataque.
- **Nenhuma execução de código** a partir de dados ingeridos: abrir trace é
  read-only; replay não executa nada (simulation/re-execution exigirão
  política explícita — spec do ciclo 05 + governança do ciclo 08).

## Resíduos aceitos / roadmap

- Assinatura de artefatos e SSO/OIDC —
  seguem fora do escopo local-first; RBAC/tenancy por API key, audit
  trail e o passthrough de TLS do `serve` já existem (ciclo 08).
  ciclos de governança/deploy. Registrados, não escondidos.
