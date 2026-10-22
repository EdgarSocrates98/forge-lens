# Segurança — Threat Model

## Ativos

- Conteúdo de execuções de agente (prompts, args de tool, outputs, tokens).
- Integridade do store (dedupe/provenance corretos).
- Disponibilidade do processo local.
- Segredos transportados pelos dados (chaves de API, tokens, credenciais
  de conexão) — devem sobreviver à ingestão apenas em forma redigida.

## Atores e superfícies

| Superfície | Ameaça | Mitigação atual |
|---|---|---|
| `POST /v1/traces`, `/v1/otlp/traces` | injeção de payloads malformados | validação de contrato + parser tolerante + limites (413) |
| mesma | bombas de aninhamento JSON (`[[[[…]]]]`) | guarda lexical de profundidade (512 níveis) antes do parse → 400; jiter (rota nativa) ainda rejeita ~255+ por conta própria → 400. Nunca RecursionError→500 |
| mesma | strings gigantes / valores longos | `max_payload_bytes` → 413; truncamento a `max_attr_value_chars` com marcador `[TRUNCATED]` explícito |
| mesma | arrays/objetos largos (largura, não profundidade) | cap de bytes + validação de contrato → 422 |
| mesma | inteiros acima de int64 (timestamps, `intValue`, query params) | bounds int64 em `_query_i64`/`to_unix_nano`/`_optional_body_datetime_ns` → 422; `intValue` arbitrário é persistido como número JSON (não bind) — sem 500 |
| mesma | floats não finitos (`NaN`, `1e400`, `Infinity`) | `_json_finite` coage a `null` na persistência; `_reject_non_finite`/duração → 422 |
| mesma | UTF inválido, `\ud800` solitário (WTF-8) | jiter recusa no nativo → 400; `json.loads` aceita → `_reject_unencodable`/`_eval_str` → 422. Nunca bind error |
| mesma | confusão de content-type | OTLP recusa `protobuf`/`octet-stream` → 415; ambas as rotas parseiam JSON independente do tipo declarado (postura documentada — ver resíduos) |
| mesma | flood/DoS | `rate_limit_per_minute` (fixo em processo) → 429 + caps de payload/traces/spans |
| mesma | ingest não autorizado (porta exposta) | `api_key`/`auth_mode` + bind `127.0.0.1` default + aviso no `serve` quando o bind não é loopback sem `api_key` |
| atributos e texto livre (`status.message`, `span.name`, `event.name`, `source.*`, `session_id`, `project_id`) | segredos em valores | `SENSITIVE_KEY_RE` nas chaves + `VALUE_PATTERNS` nos valores: `sk-*`/`sk-ant-*`/`AIza*`/`AKIA*`/`ghp_*`/`github_pat_*`/`glpat-*`/`npm_*`/`pypi-*`/`hf_*`/`xox*`/`xapp-*`/PEM/`Bearer x`/JWT **e, desde ciclo 13.1, `Authorization: <scheme> <cred>` (Basic/Digest/AWS4), userinfo de URL `scheme://user:pass@host` e formas `password=`/`token:`/`api_key=` (snake e camelCase, inclusive serializadas como `"k": "v"`)** |
| corpos de prompt/tool | PII/segredos persistidos | `capture=metadata` descarta por default com `{_forge_lens.dropped, _approx_bytes}`; e-mails mascarados (`[MASKED:email]`) |
| leitura `/v1/traces*`, `/v1/datasets`, `/v1/experiments`, `/v1/ops/summary` | vazamento a terceiros locais | `none`: aberto (modelo local); `keys`: exige `reader`+ e honra escopo `projects[]` — `project_id` omitido resolve para o default configurado, nunca para leitura global silenciosa |
| paths de rota (`{trace_id}` etc.) | traversal por URI-encodado | parâmetros são *dados* bindados a lookup — `%2e%2e`, `..%2f`, `%00` → 404, nunca alcance a filesystem |
| query strings (`q`, `tool`, `model`…) | injeção de padrão LIKE | filtros escopados por campo + escape de curingas |
| `GET /v1/traces/{id}/incident` | eco de corpos capturados em bundle derivado | `redact_export` re-aplica todo o scrub no boundary de export, com cap de profundidade 8 (`[DROPPED:depth-limit]`) e contadores `export_sanitize` divulgados |
| MCP stdio (`lens.*`) | ferramenta local maliciosa / frames hostis | gate `initialize` antes de qualquer método (exceto `ping`); TypeError de argumento → `-32602 "invalid params"` estático; outras exceções → `-32603` com só o nome do tipo — texto de exceção (com fragmentos de payload/paths) nunca cruza o fio |
| admin (`/v1/admin/*`, `/v1/alerts/rules`) | elevação de papel / escopo | `admin_deps` em toda a superfície; admin com escopo só cria/revoga/lista chaves dentro do próprio escopo e não lê o audit trail (pode conter ruído cross-tenant) |
| chaves gerenciadas | vazamento de DB ⇒ chaves usáveis | só SHA-256 do segredo é persistido; o segredo `flk_sk_*` é exibido uma única vez no mint |
| `BlobRef` | SSRF por resolução de URI | referências nunca resolvidas pelo servidor |
| SDK→API | MITM localhost | HTTP local; TLS fica a cargo do operador em exposição real |
| dependências | supply chain | deps mínimas + `pip-audit` no CI + dependabot |
| store | corrupção/injeção SQL | parâmetros sempre bindados; SQL estático |
| processo | exfiltração pelo produto | sem telemetria/saída externa por design |

## Riscos confirmados eliminados (bateria ofensiva do ciclo 13.1)

Cada item abaixo tem teste que reproduz o ataque e verifica o veredito
(`tests/unit/test_security_audit.py`):

- `RecursionError`→500 em JSON profundo: guarda lexical + tetos nativos
  dos parsers — qualquer profundidade responde 4xx.
- `OverflowError`→500 por int64 (offset, durações, timestamps, `intValue`).
- Bind 500 por surrogate solitário em qualquer rota com `json.loads`
  tolerante (OTLP, `_eval_body` de datasets/experiments/eval).
- Vazamento de texto de exceção no MCP: `-32602`/`-32603` carregam
  mensagem estática ou apenas `type(exc).__name__`.
- Escape de tenant por `project_id` omitido ou cross-project: 403
  uniforme em traces/datasets/experiments/ops/sessions/tools.
- Modo `auth_mode` desconhecido com postura ambígua (leituras abertas +
  escritas mortas): **corrigido** — `create_app` aborta na inicialização
  com `ValueError`.
- Credenciais em forma de URL (`user:pass@host`), `password=x`,
  `Authorization: Basic <blob>`, `X-Api-Key: <v>` sobrevivendo à
  sanitização: **corrigido** — três padrões novos em `VALUE_PATTERNS`,
  com lookalikes seguros (`host:8080`, `sort_key=x`, `input_tokens=n`,
  `user@host` sem esquema) verificados intocados.
- Escalação por papel plantado: registro com `role` fora do conjunto
  `reader|writer|admin` falha fechado (403 em toda rota protegida).
- Segredo apresentado como hash/key_id, segredo revogado, header JSON
  com `{"role":"admin"}`: todos 401 — nunca identidade.
- Traversal por path URI-encodado: 404 — o parâmetro nunca vira caminho.

## Decisões assumidas (declaradas)

- **Leitura aberta** apenas em `auth_mode=none` (default local
  single-user); com `auth_mode=keys` (`FORGE_LENS_AUTH_MODE` ou
  `config.toml`) toda rota exige `X-Forge-Lens-Key` com papel adequado.
  Expondo a porta, a proteção final é sempre de rede/proxy.
- **`api_key` sozinha** (`auth_mode=key`) protege apenas *escrita* —
  leituras continuam abertas a menos que `auth_reads=true`. É a postura
  explícita local-first, não um descuido.
- **OTLP protobuf recusado** — reduz parser e superfície de ataque.
- **Nenhuma execução de código** a partir de dados ingeridos: abrir trace é
  read-only; replay não executa nada (simulation/re-execution exigirão
  política explícita — spec do ciclo 05 + governança do ciclo 08).
- **IDs estruturais nunca são mascarados** — `trace_id`/`span_id`/
  `session_id`/`project_id`/`actor`/`experiment_id` passam pelo scrub de
  credenciais mas ficam imunes ao e-mail-mask: mascarar chave de join
  quebraria correlação silenciosamente (regra uniforme de `sanitize_trace`
  e `redact_export`).

## Resíduos aceitos

- **`capture=full` persiste corpos verbatim** — opt-in explícito; o scrub
  de valores continua ativo *dentro* dos atributos de conteúdo (formas de
  credencial morrem mesmo em `input.value`), mas texto livre e e-mails
  ficam intactos. É a semântica do nível, documentada no config.
- **Redação é por *forma* de credencial** — `senha é hunter2` escrito em
  prosa (sem `=`/`:`/userinfo/JWT etc.) sobrevive em qualquer nível.
  Leetspeak (`p4ssw0rd`) e codificações arbitrárias (base64 sem rótulo)
  também — enumerá-los é ilimitado por desenho.
- **Parse agnóstico de content-type** — `POST` com JSON válido é aceito
  mesmo com `text/plain` ou sem cabeçalho (rotas nativa e OTLP). Binários
  explícitos (`protobuf`, `octet-stream`) são recusados com 415 somente na
  rota OTLP. Consequência: um browser no mesmo host pode *escrever* traces
  forjados numa instância aberta via fetch/form — loopback CSRF de escrita.
  Mitigação disponível: `api_key`/`auth_mode=keys` (a chave não chega ao
  browser). Severidade: baixa no modelo local-first.
- **Rate limiter é em-processo, janela fixa, por instância** — reinício ou
  multi-worker reseta/duplica a janela. Adequado para single-user local;
  não é um anti-abuse de borda.
- **Audit trail cobre ações administrativas e negações de auth/scope** —
  `key.create`, `key.revoke`, `traces.prune`, `alert_rule.*`, `auth`/
  `scope` denied. Mutações de dados (ingest, `PUT` datasets/experiments)
  e leituras *não* geram trilha — uma writer key válida altera dados sem
  registro. Aceito no modelo local; exigir trilha forense completa é
  roadmap.
- **MCP stdio herda leitura plena do DB** — o observador local é um
  processo do operador com `readonly=True`; a fronteira de confiança é
  a do filesystem, não de autenticação no stdio.
- **Resposta de erro de validação é verbosa e não limitada** — ver risco
  identificado abaixo.

## Riscos identificados (ciclo 13.1 — auditoria ofensiva)

- **[média → eliminada] Amplificação de resposta em erros de validação de ingestão.**
  Um corpo ~500 KB com ~100 mil itens malformados produz resposta 422 de
  ~17,5 MB (~35× o pedido); no cap de 8 MB a amplificação teórica chega
  perto de ~280 MB por request. `str(exc)` serializa *todos* os erros
  Pydantic no `detail`. Impacto: custo de serialização/largura de banda
  por request hostil — não trava o processo, mas é um multiplicador de
  DoS trivial em porta exposta. **Corrigido no ciclo 13.1**:
  `_bounded_validation_detail` em `api/app.py` limita o `detail` a 25
  erros, ecoa no máximo 200 chars de input por erro e declara o total
  real — mesma disciplina do `BoundedList` de warnings.
- **[baixa] Escrita forjável em instância aberta via loopback** —
  já descrito em resíduos (parse agnóstico de content-type).
- **[informativo] `X-Forge-Lens-Key` via query não existe** — a chave só
  é lida de header; não vaza em logs de URL por desenho.
