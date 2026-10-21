# Cycle 01 — Addendum: Correções de Review (P0/P1)

**Status**: `FIXED` · **Base**: `235a2aa` (revisado e rejeitado)
**Escopo**: 10 achados críticos + warnings do code review. Nenhuma
funcionalidade de ciclos futuros adicionada — só correções e regressões.

## Como cada achado foi resolvido

| # | Achado | Correção | Regressão (teste) |
|---|---|---|---|
| 1 | Spans alterados caíam como "duplicados"; traces parciais nunca completavam | `upsert_trace` compara `span_hash`: idêntico → `duplicate` (+ flag `DUPLICATE` como evidência de retry); alterado → merge (`end_time`, parent, attrs, eventos, links, status por severidade) → `updated`. Retorno virou `(inserted, updated, duplicates)` | `test_late_end_time_merges_not_duplicates`, `test_trace_status_recomputes_from_stored_spans`, `test_late_end_time_update_counts_updated`, `test_native_ingest_reports_updated_spans` |
| 2 | SDK perdia telemetria e propagava falha de export para a app | Py: lote só sai de `_pending` após export OK; auto-flush engole falha (`export_errors` + `on_export_error`); `flush()` explícito propaga mas preserva lote. TS: idem + `.catch()` no fire-and-forget (sem unhandled rejection) | `test_flush_failure_keeps_pending_for_retry`, `test_auto_flush_export_failure_is_swallowed`, `test_auto_flush_never_masks_app_exception`, `test_on_export_error_callback_receives_trace`, TS `keeps the batch pending…`, `auto-flush rejection…`, `auto-flush failure does not mask…` |
| 3 | `span.links[*].attributes` não sanitizados | `sanitize_trace` itera `span.links` junto a spans/eventos/resource | `test_sanitize_trace_covers_link_attributes` |
| 4 | Denylist perdia `*.token`/`*_token` genéricos | `SENSITIVE_KEY_RE` ganhou sufixo `(^|[-_.])token$` — chaves de métrica (`llm.token_count.*`, `*_tokens`, `token_usage`) não terminam em `token` e seguem intactas. Valores: `glpat-`, `npm_`, `pypi-`, `hf_`, `gh*_`, `github_pat_`, `xapp-`, `xox*` | `test_generic_token_keys_redacted`, `test_metric_token_keys_are_not_redacted`, `test_credential_value_patterns` |
| 5 | CI: `uv run python -m build` sem `build` instalado | Step agora é `uv build` (que não depende do env); `AGENTS.md` idem | CI verde no push |
| 6 | `upsert_trace` não era atômico (autocommit) | `isolation_level=None` removido — `with conn:` vira transação real; falha no meio do lote faz rollback de spans + trace | `test_writes_run_inside_transaction` (spy prova `in_transaction`), `test_upsert_is_atomic` |
| 7 | `traceparent` sampled lido do último char hex | `int(flags,16) & 0x01` (py) / `parseInt(flags,16) & 0x01` (ts) | `test_traceparent_sampled_is_a_bit_not_a_char` (py+ts) |
| 8 | Primitivos OTLP malformados → HTTP 500 | `_attr_value`/`_parse_time` levantam `ValidationError` controlado; normalizer ganhou modo *quarantine* por span (`errors=[]`) — span ruim vira warning, lote segue | `test_otlp_bad_intvalue_*`, `test_otlp_bad_timestamp_*`, `test_otlp_quarantine_keeps_valid_spans`, `test_otlp_malformed_span_quarantined_not_500` |
| 9 | Sem checagem `span.trace_id == trace.trace_id` | Pipeline rejeita o lote inteiro com `ValidationError` antes de escrever (422) — nada é armazenado sob chave errada | `test_span_trace_id_mismatch_rejected`, `test_span_trace_id_mismatch_is_422` |
| 10 | `POST /v1/traces` ignorava `max_payload_bytes` | Endpoint lê o corpo bruto e aplica o limite antes do parse (413) | `test_native_payload_size_limit`, `test_otlp_payload_size_limit` |

## Warnings endereçados

- **Import side-effects**: `app` virou lazy via `__getattr__` de módulo —
  `import forge_lens.api.app` não cria `.forge-lens/` nem lê config
  (`test_import_has_no_db_side_effect`).
- **Wall-clock no storage**: todos os `datetime.now()` removidos de
  `storage/`; `first_seen`/`last_seen` derivam dos dados da trace; fallbacks
  que fabricavam timestamps em colunas NOT NULL removidos
  (`test_wall_clock_free`).
- **Precisão ns**: `to_unix_nano`/`from_unix_nano` usam aritmética inteira;
  sub-microssegundos truncam deterministicamente (datetime só tem µs —
  documentado) (`test_unix_nano_roundtrip_is_exact`).
- **Auth**: `secrets.compare_digest` na comparação da `api_key`.
- **Empty trace**: aviso correto ("no spans"), não "no span end times"
  (`test_empty_trace_no_misleading_warning`).
- **`start_span` com `trace_id` inválido**: não corrige em silêncio —
  span ganha `forge_lens.trace_id_regenerated` + id descartado preservado
  (`test_invalid_trace_id_is_flagged_not_silently_replaced`, TS idem).
- **Listas >10 000 itens**: corte explícito com marcador
  `[TRUNCATED: N items dropped]` + `truncated_values` contado
  (`test_list_truncation_leaves_marker`).
- **`*.mime_type`**: saiu de `CONTENT_ATTRIBUTES` — é metadado e sobrevive
  a `capture=metadata` (`test_mime_type_survives_metadata_capture`).
- **Flags/métricas declaradas mas mortas**: `DUPLICATE` agora é emitida
  em re-entrega idêntica; `M_EVENTS_INGESTED`, `M_QUERY_LATENCY`,
  `M_INGEST_ERRORS` e `M_API_ERRORS` conectados onde declarados.
- **`render_tree`**: reescrito iterativo (DFS com pilha) — sem
  `RecursionError` em árvores profundas; órfãos/ciclos seguem tratados.
- **`record_usage(source=)`**: aceita `MeasurementSource` (ou string
  equivalente) — não mais stringly-typed.
- **`trace_id_hint` morto**, `import hashlib` local, hack do conftest,
  `__import__` em teste, `.gitignore` com entradas alheias, dirs vazios
  de scaffold — todos limpos.
- **HTTPExporter contra socket real**: `test_http_exporter_over_real_socket`
  sobe uvicorn em porta efêmera e fecha o loop urllib→TCP→API→SQLite.

## Estado pós-fix

97 testes py (era 61) + 11 ts (era 7) — todos verdes; `ruff` limpo;
`mypy --strict` 0 erros; schemas sem drift; `uv build` ok.

## Declarado (não escondido)

- Merge de status é monotônico por severidade: um `ok` tardio não apaga
  um `error` observado (pior caso prevalece — evidência não some).
- `IntegrityFlag.PARTIAL_TRACE`/`ORPHAN`/`OUT_OF_ORDER`/`UNKNOWN_TOKENS`
  seguem reservados no enum para regras de ciclos futuros.

---

# Addendum 2 — Segunda rodada de review

**Status**: `FIXED` · **Base**: `dc56b97` (revisado e rejeitado)
**Escopo**: 3 achados críticos + warnings. Só correções e regressões.

## Críticos

| # | Achado | Correção | Regressão (teste) |
|---|---|---|---|
| 1 | Pacote TS não importava instalado (`exports`/`main`/`types` → `dist/index.js`, mas `rootDir:"."` emite `dist/src/`) | `package.json` aponta para `dist/src/*`; `files` = `dist/src` (testes fora do tarball); smoke `test/smoke-entry.mjs` resolve `@forge-lens/sdk` via self-reference — verificado também com `npm pack` + `npm install` + `import()` | `npm test` roda o smoke; reprodução manual em `/tmp` OK |
| 2 | `flush()` mid-flight apagava `_root_ctx`/`roots` → auto-flush nunca mais disparava; sem `shutdown()`/`atexit` → telemetria presa em `_pending` morria no exit | Pending só sai do mapa quando a trace **termina** (raiz com `end_time`, ou todos os spans encerrados se não há raiz registrada). `shutdown()` = flush terminal; `atexit` (py) / `beforeExit` (ts) fazem flush best-effort — ambos desligáveis (`register_atexit=False`/`shutdownHook:false`) | `test_mid_flight_flush_keeps_root_auto_flush` (py), `test_shutdown_flushes_incomplete_traces`, TS `mid-flight flush() keeps root auto-flush registered`, `shutdown() exports traces still pending` |
| 3 | `@instrument_tool(capture_args=True)` documentado mas inerte | O decorador agora encaminha `{"args":…,"kwargs":…}` para `tool_call` → `tool.parameters` (JSON ≤4 KB) | `test_instrument_tool_capture_args_records_parameters` |

## Warnings endereçados

- **OTLP `kind` como enum-name** (`"SPAN_KIND_SERVER"`): `map_span_kind`
  aceita int e string (case-tolerant), consistente com `_parse_status`;
  OpenInference segue com precedência (`test_otlp_enum_name_span_kind`).
- **SDK Python sem thread-safety**: `RLock` em `_pending`/`_root_ctx`/
  `trace.spans`; export usa `model_copy(deep=True)`; callbacks fora do
  lock. TS: flushes serializados por cadeia de promises; snapshot via
  `structuredClone` (`test_concurrent_spans_during_export_are_not_lost`).
- **`TraceStore.upsert_trace` sem guarda própria**: agora rejeita
  `span.trace_id != trace.trace_id` com `ValidationError` antes da
  transação — defesa em profundidade além do pipeline
  (`test_store_rejects_span_trace_id_mismatch`).
- **Poison batch (413 permanente)**: lote multi-trace que falha passa por
  *salvage* (re-export por trace); falhas consecutivas por trace são
  contadas — após `max_export_failures` a trace vai para `dead_letters`
  e `on_dead_letter` dispara. Explícito, inspecionável, não bloqueia o
  resto (`test_poison_batch_salvages_good_traces`,
  `test_persistent_failure_dead_letters_trace`, TS idem).
- **`traceparent`**: versão `ff` e ids all-zero rejeitados nos dois SDKs
  (`test_traceparent_rejects_ff_version_and_zero_ids` + TS).
- **`parentSpanId` all-zero** → tratado como ausente, não gera
  `MISSING_PARENT` fantasma (`test_otlp_all_zero_parent_span_id_is_absent`).
- **`started_ns or seen_ns`** → `is not None` explícito: época Unix (0)
  não é mais confundida com "sem dado"
  (`test_epoch_zero_started_ns_is_not_treated_as_missing`).
- **Envelope `project_id`**: a regra agora é *envelope preenche, não
  sobrescreve*: `project_id` não-default do envelope só se aplica a traces
  que estão `"default"`; per-trace explícito é autoritativo. `--project`
  do CLI continua override explícito (`test_envelope_project_id_fills_*`,
  `test_default_envelope_preserves_explicit_trace_project`).
- **`time.monotonic()` no pipeline**: `RateLimiter`/`IngestPipeline`
  aceitam `clock` injetável — janela determinística sob teste
  (`test_rate_limiter_uses_injected_clock`).
- **`graphify-out/`**: removido e git-ignorado; contagem do addendum 1
  corrigida (97, não 95).

## Estado pós-fix (rodada 2)

111 testes py (era 97) + 16 ts (era 11) + smoke de entrada do pacote —
todos verdes; `ruff` limpo; `mypy --strict` 0 erros; schemas sem drift;
`uv build` ok; `pip-audit` 0 vulns; `npm pack`→install→import verificado.

## Declarado (não escondido) — atualização

- Dead-letter é por-trace com contador de falhas consecutivas — não há
  backoff exponencial nem fila persistente em disco (o `dead_letters`
  fica em memória; `on_dead_letter` é o gancho para persistir).
- Flush segura o lock durante `export()` — simples e correto; um exporter
  lento bloqueia `start_span` de outras threads (custo aceito para o
  perfil local-first; exporters devem ter timeout próprio).
- `atexit`/`beforeExit` são best-effort — não há garantia de flush em
  `SIGKILL`/crash; `shutdown()` explícito é o caminho garantido.

## Addendum 3 — Terceira rodada de review

Fixes aplicados sobre os achados da terceira revisão (o item "roadmap
incompleto" é decisão de escopo: o prompt manda começar pelo Cycle 00 e
veda PR gigantesco único — os Cycles 02–12 permanecem `DOCUMENTED_ONLY`
e não foram afetados por esta rodada).

### Críticos

- **`POST /v1/otlp/traces` com body JSON não-objeto → 500**: arrays,
  strings e números válidos-JSON caíam em `payload.get` →
  `AttributeError`. Agora `isinstance(payload, dict)` no endpoint → 422;
  `normalize_otlp_request` também guarda (assinatura `payload: Any`) —
  `test_otlp_non_object_body_returns_422` + `test_otlp_non_dict_payload_rejected`.
- **500 em datetimes mistas naive/aware**: span `end_time` naive vs
  `start_time` aware quebrava `flag_integrity`; `sent_at` naive idem em
  `app.py`. Novo `ensure_aware()` (`core/time.py`, naive-as-UTC —
  mesma convenção de `to_unix_nano`) aplicado a todos os tempos da
  trace no pipeline (`_normalize_times`) e ao `sent_at` do envelope;
  `_finalize` do SDK Python recebeu a mesma defesa —
  `test_native_mixed_naive_aware_times_not_500`,
  `test_native_naive_sent_at_not_500`, `test_naive_times_normalized_to_utc`.

### Warnings

- **Colunas de trace estagnadas no upsert**: `status_message` agora faz
  `COALESCE(excluded, stored)` (a mais recente não-nula vence);
  `resource_attributes_json` une por chave (`json_patch`); `source_json`
  cede a `system` identificado (nunca regride a `"unknown"`);
  `provenance` mantém a mais forte observada (nunca promove) —
  `test_upsert_merges_trace_level_columns`, doc `storage.md` atualizada.
- **Sem bound de traces por request**: `max_traces_per_request`
  (default 10 000; `LensConfig` + toml + `FORGE_LENS_MAX_TRACES_PER_REQUEST`)
  → 413 antes de qualquer escrita —
  `test_max_traces_per_request_limit` (API) + `_bounded` (pipeline).
- **Evidência de duplicata inconsistente**: o caminho merge→None não
  registrava `DUPLICATE` nem flags incoming-only. Novo `_union_flags`:
  união de flags + `DUPLICATE` em *ambos* os caminhos de duplicata —
  `test_duplicate_merge_identical_content_still_records_flags`.
- **Eventos OTLP sem `timeUnixNano` descartados em silêncio**: a contagem
  viaja no span (`attributes["_otlp.events_dropped"]`) e em quarantine
  entra na lista `errors` — `test_otlp_event_without_time_is_evidence_not_silent`
  + `test_otlp_event_drop_recorded_in_strict_mode_too`.
- **Foot-gun de bind exposto**: `serve` avisa no stderr quando `--host`
  não é loopback e `api_key` está vazia (leituras `/v1/traces*` são
  abertas por design local) — `test_serve_warns_on_exposed_bind`,
  doc `cli.md`/`threat-model.md`.

### Sugestões

- **`finalize()` TS com sort lexicográfico de ISO**: strings com offset
  (`+HH:MM`) ordenavam errado; `cmpIso` compara via `Date.parse` com
  fallback lexical — `finalize orders offset ISO timestamps by instant`.
- **`examples/` ausente**: `examples/quickstart.py` — run completo
  offline (SDK → pipeline → store → leitura), verificado executando.
- **Contagem de testes desatualizada no final report**: anotada
  (61 → 123 py / 17 ts após as rodadas de review).

## Estado pós-fix (rodada 3)

123 testes py + 17 ts + smoke de pacote — verdes; `ruff` limpo;
`mypy --strict` 0 erros; schemas sem drift; `uv build` ok;
`pip-audit` 0 vulns; `examples/quickstart.py` executado com sucesso.

## Rodada 4 — achados → correções

### Warnings

- **`kvlistValue` malformado → HTTP 500**: `kv.get("key","")` virava chave
  de dict sem guarda; `key` array/objeto → `TypeError: unhashable` → 500.
  `_attr_value` agora só aceita entradas `dict` com `key` `str` —
  `test_otlp_kvlist_nonstring_key_quarantined`.
- **Chaves `*Token` camelCase fora da redação**: `(^|[-_.])token$` não
  casava `accessToken`/`refreshToken`/`idToken`/`bearerToken`/… → token
  opaco persistia no default `capture=metadata`. Suffix virou `token$`
  (métricas `*.prompt`/`_usage`/`_tokens` seguem fora — terminam em
  `prompt`/`usage`/`tokens`, não `token`) —
  `test_camelcase_token_keys_redacted` +
  `test_generic_token_keys_redacted` (regressão das métricas).
- **Sanitização não varria campos de texto livre**: `status.message`
  (span+trace), `span.name`, `event.name`, `session_id` e `source.*`
  (`system`/`sdk_name`/`sdk_version`/`runtime`/`language`) persistiam
  credenciais — `status.message=str(exc)` rotineiramente embute segredos.
  `_scrub_text` aplica os padrões de valor (sk-*/ghp_*/glpat-*/… +
  mascaramento de e-mail, exceto `session_id` — chave de lookup,
  e-mail-mask quebraria a identidade de join) —
  `test_status_message_and_names_scrubbed`.
- **Reentrega conflitante rotulada `DUPLICATE`**: mesmo
  `(project,trace,span)` com `name`/`kind`/`start_time` diferentes
  mergeava→`None` e levava `DUPLICATE` — evidência de "replay idêntico"
  falsa, conteúdo conflitante descartado sem registro. Novo
  `IntegrityFlag.CONFLICT`: identidade divergente → `CONFLICT` (merge
  preserva first-observed; a divergência fica registrada); hash
  idêntico ou merge→idêntico com mesma identidade → `DUPLICATE` —
  `test_conflicting_redelivery_flags_conflict_not_duplicate` +
  `test_conflicting_update_merge_marks_conflict`.
- **Imports de arquivo com `provenance=observed`**: `ingest file.json`
  agora passa `Provenance.IMPORTED` a `normalize_otlp_request`
  (enum já definia `imported` = "received from an external source
  (file import…)") — `test_cli_otlp_import_marks_provenance_imported`.

### Menores

- **`load_config` crashava com `ValueError` cru** em env malformado
  (`FORGE_LENS_CAPTURE=bogus`, `FORGE_LENS_MAX_TRACES_PER_REQUEST=abc`)
  → agora `ValueError("invalid env FORGE_LENS_*=…")` contextual —
  `test_load_config_bad_env_reports_variable_name`.
- **`ingest --format` aceitava string arbitrária**: valor fora de
  `native|otlp|auto` → exit 2 explícito em vez de cair no caminho
  native — `test_ingest_rejects_unknown_format`.
- **Lazy init de `get_store`/`get_pipeline` raceável**: `threading.RLock`
  em torno da criação (RLock porque `get_pipeline` chama `get_store`
  sob o mesmo lock — Lock simples deadlockearia, reproduzido em teste).
- **`stats()["db_size_bytes"]` ignorava sidecars**: agora soma
  `lens.db` + `lens.db-wal` + `lens.db-shm` —
  `test_db_size_includes_wal_sidecars`.
- **Docstring OTLP afirmava rejeição de protobuf que não existia**:
  `ingest_otlp` agora retorna 415 para `Content-Type` protobuf/
  octet-stream antes de tentar parse —
  `test_otlp_protobuf_content_type_rejected_415`.
- **Métricas de erro inconsistentes**: `_reject_ingest` centraliza —
  413/415/422/429/400 incrementam `M_API_ERRORS` + `M_INGEST_ERRORS`
  uniformemente — `test_ingest_rejections_count_as_ingest_errors`.

## Estado pós-fix (rodada 4)

134 testes py + 17 ts + smoke de pacote — verdes; `ruff` limpo;
`mypy --strict` 0 erros; schemas regeneradas (`CONFLICT` no enum) sem
drift extra; `uv build` ok; `pip-audit` 0 vulns; `examples/quickstart.py`
executado com sucesso.

Nota honesta: `CONFLICT` registra a divergência nos flags do span
persistido — o primeiro `name`/`kind`/`start_time` observado continua
sendo a verdade mantida (first-observed wins), e a entrega conflitante
fica evidenciada pelo flag. Não há `warnings` no `IngestResult` para
conflito porque a dedupe acontece no store, abaixo do pipeline.

## Rodada 5 — achados → correções

### Críticos

- **`traces.ended_ns` nunca preenchido após entrega parcial**: `MAX(a, NULL)`
  retorna NULL no SQLite — trace armazenada parcial (flush mid-flight)
  ficava `ended_at=None` para sempre mesmo com a redelivery completa.
  `COALESCE` aplicado nos dois lados do `MAX` —
  `test_partial_trace_completes_ended_at_filled`.
- **OTLP nesting profundo → HTTP 500**: `_attr_value` recursivo sem cap;
  ~2.000 níveis (60 KB) → `RecursionError` fora do handler de quarentena.
  `_MAX_ANYVALUE_DEPTH=32` → `[DROPPED:depth-limit]` no atributo —
  `test_otlp_deeply_nested_anyvalue_does_not_crash` +
  `test_otlp_deeply_nested_anyvalue_not_500` (e2e).
- **Decoradores corrompiam telemetria em `async def`**: `@span`/
  `@instrument_tool` mediam a *criação* da corrotina (~0 ms) e
  mis-parentavam spans internos. `inspect.iscoroutinefunction` /
  `isasyncgenfunction` / `isgeneratorfunction` emitem wrappers
  async/generator que cobrem o corpo real (contextvars isolam por task) —
  `test_span_decorator_covers_async_body`, `test_instrument_tool_async`,
  `test_span_decorator_covers_generator_iteration`.

### Warnings

- **`await request.body()` antes do size check**: `Content-Length` é
  checado primeiro (fast-path 413 sem buffer) — a verificação pós-leitura
  permanece (chunked/lie) — `test_oversized_content_length_rejected_before_read`.
- **`end_time` fora de ordem regredia o span**: `incoming or stored` →
  `max(stored, incoming)`; entrega com `end_time` anterior registra
  `OUT_OF_ORDER` (enum existia, nunca emitido) —
  `test_older_end_time_does_not_regress_and_flags_out_of_order`.
- **`_merge_events` descartava attrs divergentes**: e pior — mutava o
  objeto stored, então o check `merged==stored` não via a diferença e o
  merge nunca persistia (mesmo bug latente em `_merge_links`). Ambos
  agora produzem cópias — `test_same_key_event_attributes_merge`.
- **Sanitize gaps**: `source.host` e `project_id` agora passam pelos
  padrões de credencial (sem e-mail-mask — chaves de lookup/partição);
  drop por `depth>8` conta em `SanitizeReport.dropped_content` —
  `test_project_id_and_source_host_scrubbed`, `test_depth_limit_drop_is_counted`.
- **`cfg.endpoint` era config morta**: `serve` deriva host/port do
  endpoint configurado; `--host`/`--port` sobrescrevem; endpoint
  malformado → exit 2 com contexto — `test_serve_binds_from_endpoint_config`,
  `test_serve_rejects_malformed_endpoint`.
- **Status de trace sem spans descartado**: o rollup só por SELECT
  zerava para `unset`; `COALESCE(subquery HAVING COUNT(*)>0,
  NULLIF(excluded,'unset'), stored)` preserva o status declarado —
  `test_spanless_trace_status_not_clobbered`.
- **Guarda de wall-clock estreita**: `test_wall_clock_free` agora cobre
  `storage/`+`ingest/`+`api/`+`otlp/`+`sanitize/` (monotonic/
  perf_counter permitidos — timers de intervalo, injetáveis).

### Sugestões

- **Env mapping incompleto**: `FORGE_LENS_MAX_PAYLOAD_BYTES`,
  `_MAX_SPANS_PER_REQUEST`, `_MAX_EVENTS_PER_SPAN`, `_MAX_ATTR_VALUE_CHARS`
  agora mapeados (int, com erro contextual) —
  `test_env_covers_all_int_bounds`, `test_env_bad_int_reports_variable_name`.
- **`tool.parameters` truncado mid-string**: acima de 4 KB vira
  `{"_forge_lens.truncated": true, "_approx_bytes": N}` — JSON sempre
  parseável — `test_tool_parameters_truncation_stays_parseable`.
- **`get_trace` cross-project**: documentado em `api.md` (partição, não
  fronteira).

## Estado pós-fix (rodada 5)

151 testes py + 17 ts + smoke de pacote; `ruff` limpo; `mypy --strict`
0 erros; schemas sem drift; `uv build` ok; `pip-audit` 0 vulns.

## Rodada 6 — auth 500, TOML corruption, ingest try-scope

### Warnings (confirmados)

- **`X-Forge-Lens-Key` não-ASCII → HTTP 500**: `secrets.compare_digest`
  em `str` levanta `TypeError` com entrada não-ASCII — header
  controlado pelo atacante recebia 500, não 401. Comparação agora em
  bytes (`str.encode()` dos dois lados) —
  `test_auth_non_ascii_header_401_not_500`.
- **`init --project 'bad"name'` corrompia o TOML**: `project_id` era
  interpolado crú em `config.toml` → `project_id = "bad"name"` →
  `TOMLDecodeError` em todo comando seguinte. Serialização via
  `json.dumps` (mesmo conjunto de escapes do TOML basic string) —
  qualquer id, incluindo aspas e chars de controle, é literal válido —
  `test_init_project_with_quotes_writes_valid_toml`.
- **`ingest` lia/parsava o arquivo fora do `try`**: `file.read_text` +
  `json.loads` em arquivo inexistente/malformado produzia traceback cru
  em vez do caminho `ingest failed: {exc}` que o próprio comando
  estabeleceu. Ambos movidos para dentro do `try` —
  `test_ingest_missing_file_uses_formatted_error`,
  `test_ingest_malformed_json_uses_formatted_error`.

### Sugestões

- **Doc imprecisa em `api.md`**: `GET /v1/traces/{id}` sem `project_id`
  sempre injetava `cfg.project_id` — corrigido (leitura cruzada só no
  `Store` interno).
- **`utc_now()` morto**: removido de `core/time.py` (nunca chamado; a
  regra de wall-clock o tornava suspeito).
- **Proveniência de span não subia em re-entrega**: `_merge_span` não
  mesclava `provenance` e `_update_span` nem persistia a coluna —
  assimetria com a linha de trace (que mantém a mais forte). Agora
  `_stronger_provenance` + `provenance=?` no UPDATE — upgrade conta como
  `updated`, nunca `duplicate` —
  `test_span_provenance_upgrades_on_redelivery`.
- **`bytesValue` `_bytes` contava chars base64**: decodifica e conta
  bytes reais; base64 inválido cai no comprimento da string —
  `test_otlp_bytesvalue_reports_decoded_length`.
- **TS `flush()` race de microtask**: `flushRunning=false` no `finally`
  de um run antes do microtask do run enfileirado iniciar — um `flush()`
  síncrono nesse gap tomava o fast-path e intercalava. `flushQueued`
  conta no enqueue e `flushRunning` permanece true enquanto houver
  run na fila — `flush serialization` suite em `sdk.test.ts`.
- **Python `flush()` segura `_lock` em I/O de rede**: correto (snapshot
  consistente) mas pode travar `start_span` de outras threads pela
  duração do export — documentado como limitação conhecida no docstring,
  com a saída recomendada (exporter que retorna rápido e envia em
  worker próprio).
- **Redaction hooks do Cycle 01.6**: agora presentes — `redact` em
  `LensTracer` (py) e `LensTracerOptions.redact` (ts), chamado por span
  no flush antes do snapshot; erro do hook conta em `export_errors`,
  nunca quebra o app — `test_redact_hook_*` (py) + `redact hook`
  suite (ts).

## Estado pós-fix (rodada 6)

159 testes py + 20 ts + smoke de pacote; `ruff` limpo; `mypy --strict`
0 erros; schemas sem drift; `uv build` ok; `pip-audit` 0 vulns.

## Rodada 7 — remaining unguarded OTLP paths → 500

### Blocking

- **Containers OTLP malformados ainda produziam HTTP 500**: rodadas 3–5
  fecharam `intValue`, `timeUnixNano`, body não-dict, kvlist keys e depth
  cap — mas deixaram caminhos não-guardados que `TypeError`/
  `AttributeError` escapavam do catch `(ValidationError,
  PydanticValidationError)`. Corrigidos com o mesmo idioma defensivo já
  usado no arquivo:
  - `resourceSpans[].resource` não-dict → coerced a `{}` + nota em `errors`
  - `scopeSpans[].scope`/`instrumentationLibrary` não-dict → `{}` + nota
  - `scopeSpans`/`spans` não-list → skip + nota em `errors` (strict raise)
  - `span.events`/`span.links` não-list → campo marcado em
    `attributes["_otlp.malformed_fields"]` + nota (strict raise);
    links não-dict em lista contam em `_otlp.links_dropped`
  - `arrayValue.values`/`kvlistValue.values` não-list → nó retorna raw
    (evidência verbatim); `null` explícito lê como ausente
  - Regressões: `test_otlp_non_dict_resource_and_scope_not_500`,
    `test_otlp_non_list_containers_not_500`,
    `test_otlp_anyvalue_non_list_values_not_500`,
    `test_otlp_non_list_field_strict_mode_raises` +
    `test_otlp_malformed_containers_not_500` (12 shapes e2e, todos 200/422).

### Sugestões

- **`name: null` → literal "None"**: agora `raw.get("name") or "unnamed"`
  — `test_otlp_null_name_becomes_unnamed`.
- **`SanitizeReport.details` morto**: campo declarado, nunca populado —
  removido (agregação em `result.sanitize` só itera contadores).
- **TOML `rate_limit_per_minute = true`**: `bool` é subclasse de `int` —
  agora `not isinstance(v, bool)` protege os bounds int —
  `test_toml_bool_is_not_an_int_bound`.
- **`extra="allow"` não persistia**: nota honesta em `contracts.md` —
  extras validam no contrato mas não sobrevivem ao round-trip do store.
- **`GET /v1/traces` `offset` negativo**: clamped a 0 (simétrico com
  `limit` — clamp, não 422) no endpoint e no store —
  `test_negative_offset_clamped_to_zero`.
- **Bounds declarados vs. spans sem evidência**: `flag_integrity` agora
  emite warning quando `started_at`/`ended_at` do envelope contradiz o
  min/max real dos spans — `test_declared_bounds_contradicting_spans_warns`.
- **`get_tracer()` race na primeira construção**: double-checked locking
  — `test_get_tracer_singleton_is_thread_safe`.

## Estado pós-fix (rodada 7)

169 testes py + 20 ts + smoke de pacote; `ruff` limpo; `mypy --strict`
0 erros; schemas sem drift; `uv build` ok; `pip-audit` 0 vulns.

## Rodada 8 — revisão externa (sugestões de fundação)

Sem bugs bloqueantes; as seis sugestões do reviewer foram aplicadas:

- **`runtime` ≠ `language`**: `TraceSource.runtime` agora vem de
  `process.runtime.name`/`process.runtime.version` (semconv OTel) —
  ex. `cpython-3.12.1` — em vez de duplicar `telemetry.sdk.language`.
  Golden fixture atualizado para carregar os attrs realistas —
  `test_otlp_runtime_uses_process_runtime_attrs`.
- **Endpoint sem scheme**: `endpoint = "localhost:7777"` era lido por
  `urlsplit` com `localhost` como *scheme* e caía silenciosamente para
  `127.0.0.1:7777`. Agora sem `://` é parseado como `//host:port` —
  `test_serve_schemeless_endpoint_parses_host_port`.
- **Warning de exposição com api_key**: `_warn_if_exposed` só avisava
  quando *não* havia chave; com chave num bind não-loopback, GETs de
  leitura continuam abertos por design — agora avisa também nesse caso
  — `test_serve_warns_on_exposed_bind` atualizado.
- **`LIKE '%missing_parent%'`**: `_` é wildcard SQL; trocado por
  `json_each(...)` com match exato do elemento — seguro mesmo fora do
  enum fechado atual.
- **Rate limiter / atomicidade documentadas**: `ingestion.md` deixa
  explícito que o limiter é janela fixa (burst ~2× na fronteira) e que
  a transação é por trace, não por request (re-ingest idempotente
  torna reenvio seguro).
- **`get_tracer()` kwargs**: docstring agora declara que argumentos
  posteriores à primeira construção são ignorados (singleton).

## Estado pós-fix (rodada 8)

171 testes py + 20 ts; `ruff` limpo; `mypy --strict` 0 erros; golden
fixture exercitando `process.runtime.*`; schemas sem drift.

## Rodada 9 — revisão externa (fixes + avanço de roadmap)

Além das correções, esta rodada entregou implementação real de dois
ciclos do roadmap (ver `cycle-03-final-report.md` e
`cycle-04-final-report.md`):

- **`ingest` CLI honrava `max_payload_bytes`? Não** — `read_text`
  carregava o arquivo inteiro e `pipeline.ingest` ia sem
  `payload_bytes`. Agora `read_bytes` + `payload_bytes=len(raw)` —
  o mesmo bound da API vale no caminho de arquivo —
  `test_ingest_enforces_payload_bound`.
- **Assimetria de resposta**: `POST /v1/otlp/traces` omitia `sanitize`
  apesar de computá-lo — agora devolve o mesmo envelope do nativo —
  `test_otlp_response_includes_sanitize`.
- **`auth_reads` (fundamento Cycle 08)**: novo campo de config
  (`auth_reads = true` / `FORGE_LENS_AUTH_READS`) estende a exigência de
  `api_key` aos GETs de dados; `_warn_if_exposed` aponta a flag quando
  bind não-loopback + chave sem `auth_reads` —
  `test_auth_reads_gates_get_endpoints`,
  `test_auth_reads_off_keeps_reads_open`.

### Cycle 03 (grafo temporal) — backend

`forge_lens/graph/`: `ExecutionGraph` com arestas `parent`/`sequence`/
`link` (determinístico, `dangling` evidenciado, `missing_parents` como
estatística honesta), `critical_path`, `self_time_ranking`,
`build_session_graph`. Endpoints `/v1/graph/traces/{id}[,
/critical-path, /bottlenecks]` e `/v1/graph/sessions/{id}` (com
`stats.truncated`). CLI `forge-lens graph`.

### Cycle 04 (tokens/custo/contexto) — backend

`forge_lens/intel/`: `PricingRegistry` (tabela bundled versionada +
override `pricing_file` JSON, lookup exato→prefixo), `CostReport` por
span/modelo com `provenance: derived` + `assumptions`, `detect_waste`
com 5 findings evidenciados (`retry_amplification`, `context_replay`,
`duplicate_delivery` precificado, `unknown_tokens`, `unpriced_model`).
Endpoints `/v1/traces/{id}/cost` + `/v1/analytics/pricing`. CLI
`forge-lens cost`.

## Estado pós-fix (rodada 9)

192 testes py + 20 ts; `ruff` limpo; `mypy --strict` 0 erros em 45
arquivos; schemas sem drift (grafos/relatórios são modelos de saída,
não contratos de ingestão).

## Rodada 10 — revisão externa (crash em ciclo + flags mortas)

- **`critical_path` caía com `RecursionError`** (HTTP 500 / crash no
  CLI) em grafos com ciclo de parentesco ou cadeias profundas — a
  proteção já existia em `_max_depth` e em `traces show` (iterativo
  "span trapped in a parent cycle") e eu não a repliquei no algoritmo
  novo. Reescrito iterativo com coloring WHITE/GRAY/BLACK: a aresta que
  fecha o ciclo é cortada deterministicamente e reportada em
  `cycles[]` (evidência); avaliação cobre **todos** os roots (traces
  parciais multi-root deixavam de reportar o máximo). `stats` do grafo
  ganha `cycle_members`. Testes: `test_critical_path_survives_
  parent_cycle` (A↔B), `test_critical_path_self_parent`,
  `test_critical_path_deep_chain_no_recursion` (2000 spans),
  `test_critical_path_multi_root_takes_max`.
- **`PricingRegistry.from_file` 500 por request**: registry agora é
  construído *eager* em `create_app` — `pricing_file` malformado falha
  na construção (e em `doctor`/`serve`), não num 500 repetido por
  request. CLI `cost` embrulha o erro em exit 2 limpo —
  `test_bad_pricing_file_fails_at_construction`.
- **Flags do contrato ligadas**: `UNKNOWN_TOKENS` (span LLM sem
  `forge_lens.token_usage`), `PARTIAL_TRACE` (root quando
  `ended_at=None`), `ORPHAN` (parent presente no store sob *outra*
  trace — novo `span_trace_id` no `TraceStore`/SQLite) — testes
  `test_llm_span_without_usage_gets_unknown_tokens`,
  `test_partial_trace_flags_root_span`,
  `test_cross_trace_parent_gets_orphan`.
- **Paridade CLI ingest**: envelope `source` preenche traces com
  `source.system == "unknown"` — mesma regra da API —
  `test_ingest_fills_source_from_envelope`.
- **`_registry()` fora do lock**: tornou-se moot — eager, sem holder
  lazy, thread-safe por construção.
- **README "Status: Cycle 01"** atualizado (03/04 backend shipped).

## Estado pós-fix (rodada 10)

~200 testes py + 20 ts; `ruff` limpo; `mypy --strict` 0 erros; schemas
sem drift; `cycle_members`/`cycles` são evidência em stats — nenhum
campo novo em contratos (enum intacto). Na mesma rodada, Cycle 05
backend (`forge_lens/investigate/`): `classify_failure`,
`replay_timeline`, `incident_bundle`, endpoints `/failure` `/replay`
`/incident`, CLI `forge-lens incident` — relatório em
`cycle-05-final-report.md`.

## Rodada 11 — correções em Cycle 05 + hardening de ciclos

- **`replay_timeline` vazio sem chaves (crítico)**: trace zero-span
  retornava sem `total_ms`/`unclosed_spans` → `KeyError` no CLI
  `incident` e schema inconsistente em `/replay`. Ambas sempre emitidas.
- **`CYCLE` só via self-parent**: agora qualquer ciclo real —
  `cycle_member_ids` (walk funcional O(n), WHITE/GRAY/BLACK) público em
  `graph/builder.py`, reutilizado pelo classifier e pelo stat
  `cycle_members`. `failed=true` para topologia impossível.
- **`_cycle_members` O(n²)**: o walk por nó era O(n·depth) (~15s em 5k
  spans, minutos em 20k = limite legal de ingest). Substituído pelo
  mesmo detector O(n).
- **`failed` em CLOCK_SKEW**: flag de data-quality não é veredicto —
  `failed = erros | não-fechados | ciclo`.
- **`rate_limit` escopo**: apenas spans com erro — 429 recuperado não
  é causa.
- **`confidence: Literal["observed","inferred"]`** — disciplina de
  contrato.
- **`UNKNOWN_TOKENS` em usage malformado**: `extract_usage() is None`
  cobre ausente **e** não-parseável.
- **`span_trace_id` determinístico**: `ORDER BY (trace_id = ?) DESC,
  trace_id` — mesma-trace primeiro (sem falso ORPHAN em entregas
  parciais) e desempate estável.
- **ORPHAN stale**: `_refresh_integrity_flags` e `_merge_span` limpam
  ORPHAN quando o parent aterrissa na mesma trace.
- **`_TIMEOUT_RE`**: "exceeded" nu removido ("context length exceeded"
  não é timeout); `_msg` inclui `exception.type`.
- **Dead GRAY check** em `critical_path` removido (pushes só em WHITE).

Testes: +12 (ciclos multi-nó/self/lead-in, cadeia 5k, skew≠falha,
429 recuperado, context-length≠timeout, exception.type, replay vazio,
ORPHAN resolvido/falso, usage malformado).

## Rodada 12 — recursos verdadeiramente limitados + stats corretos

- **Body sem bound (crítico)**: `request.body()`/`request.json()`
  bufferizavam o payload inteiro antes do check — um POST chunked/sem
  Content-Length de qualquer tamanho era materializado. Novo
  `_read_capped_body` lê em streaming e aborta no primeiro byte acima
  de `max_payload_bytes` (o `Content-Length` check vira só fast-path).
- **Amplificação de warnings**: 1 nota por item malformado → ~15× o
  payload (0.8MB → 11.6MB; 8MB → ~120MB). Novo `core/bounded.BoundedList`
  (cap 200 + nota `"... and K more item(s)"`) aplicado durante a
  coleta em `normalize_otlp_request` e em `IngestPipeline` — não é
  trim pós-hoc, a memória de coleta também é limitada.
- **`max_depth` errado em multi-root**: BFS saía só do `root_id`
  eleito → sub-reportava. Agora cobre *todos* os roots de
  `siblings[None]` + membros de ciclo como pseudo-roots (árvores
  penduradas em ciclo contam). Traces `a`(1) + `b→c→d→e` reportam 4.
- **`incomplete` ≠ in-flight**: `MISSING_END` com `ended_at=None` é
  execução em curso → `failed=false`; a categoria `incomplete`
  (`failed=true`) exige envelope fechado + spans abertos.
- **Ordem de replay**: tie-break por `kind` era alfabético — evento no
  mesmo timestamp do início do span aparecia antes de `span_start`.
  Rank explícito `span_start < event < span_end`.
- **`has_token_usage` consistente**: `extract_usage() is not None` —
  dict de usage malformado não conta como usage (mesma régua do flag
  `UNKNOWN_TOKENS`).
- **Config/pricing error UX**: `load_config` embrulha erros de TOML
  com o caminho do arquivo; `PricingRegistry.from_file` embrulha
  JSON inválido, entry não-objeto e chaves faltantes com
  arquivo+modelo.
- **`doctor` não-mutante**: `SQLiteTraceStore(readonly=True)` — conexão
  `mode=ro`, sem `mkdir`, sem `init_schema`, sem `PRAGMA journal_mode`
  (que persiste WAL). Teste de hash do arquivo antes/depois.

Testes: +8 (chunked oversize 413, quarantine cap, multi-root depth,
cycle-attached depth, in-flight≠falha, truncamento de envelope fechado,
ordem span_start/event, has_token_usage malformado, doctor imutável).

## Round 13 — semântica de evidência + Cycle 06 backend

- **Divergência de `parent_span_id` → `CONFLICT`**: `_identity_differs`
  compara só name/kind/start; um redelivery alegando outro pai era
  absorvido como DUPLICATE com o pai original preservado silenciosamente.
  Agora diverge de topologia gera `IntegrityFlag.CONFLICT` quando ambos
  os lados declaram pais diferentes (pai ausente continua merge
  first-non-None — informação nova, não contradição).
- **Flags de produtor não são confiáveis**: o pipeline zera
  `span.integrity_flags` na entrada — `conflict`/`duplicate`/
  `out_of_order`/`truncated` no wire fabricavam evidência. Todos os
  flags são vereditos do engine, recomputados na ingestão e no merge.
- **CLI ingest bounded**: `stat()` antes de ler + leitura limitada a
  `max_payload_bytes + 1` — sem `read_bytes()` irrestrito.
- **`top` clamped**: endpoint de bottlenecks clampa `0..100` como os
  demais parâmetros de consulta.
- **Dead code**: removido `parse_unix_nano` (nunca chamado).
- **Cycle 06 backend**: `forge_lens/eval/` — avaliadores determinísticos,
  `EvalReport` agregado, `compare_runs` com z-teste honesto; endpoints
  `/v1/eval/*` + CLI `forge-lens eval`. `evaluated_at` só existe quando
  injetado — relatórios reproduzíveis byte-a-byte.

Testes: +10 (parent-divergence CONFLICT, producer-flag strip, eval
battery ×6, endpoints eval ×2).

## Round 14 — limites e 4xx nos endpoints de eval

- **`json.loads` sem guarda** em `eval/run` e `eval/compare` → 400
  (mesmo padrão de `ingest_otlp`), não 500.
- **Validação única de shape**: `trace_ids` (1..500 strings), `evaluators`
  (1..100), `dataset`/`project_id` (str|None), sides de `compare`
  não-vazios — tudo 422 antes de tocar store/engine.
- **Specs malformados nunca 500**: `run_evaluators` valida `spec` dict e
  `params` dict — junk vira `score=None` + `details.reason`
  (`invalid_spec`/`evaluator_error`), alinhado ao docstring.
- **Specs cap 100** — fan-out de resultados fica bounded (specs × traces).
- **`_read_capped_body`/`_check_declared_size` parametrizados** com o
  reject — oversize em eval conta em `api.errors`, não `ingest.errors`.
- **`not_computable` real**: variância pooled zero (teste indefinido)
  distinguido de `insufficient_data` (n<10).
- **`sum` emitido** no summary; `tool_success` registrado binário
  (pass = todas as tools ok); `has_span` documentado.
- **`latency_budget` honesto**: `ended_at < started_at` → `score=None`
  + reason (envelope inválido não passa trivialmente no budget).
- **Dedupe consistente**: `trace_ids` repetidos avaliam uma vez;
  `EvalReport.trace_ids` reporta o conjunto distinto avaliado.
- **CLI**: `file.stat()` hoisted (era chamado 2× no caminho de erro).

Testes: +5 (bateria de 11 bodies malformados → 4xx, specs junk →
score=None, `not_computable`, negative-duration, dedupe+sum).

## Round 15 — eval boundary: os 500s confirmados fechados

- **`OverflowError` capturado**: `run_evaluators` agora cobre
  `ArithmeticError` — `int(1e999)`, `float(10**400)` viram
  `evaluator_error` com `score=None`, não 500.
- **Params não-finitos não fabricam veredito**: `budget_ms`/`max_total_tokens`
  com `Infinity`/`NaN` (que `json.loads` aceita literalmente) →
  `ValueError` → `score=None` + reason. Antes `Infinity` dava pass grátis
  e `NaN` fail universal — sinais confiantes de specs malformados.
- **Lone surrogates → 422**: `json.loads` materializa surrogates de
  escapes `\ud800` válidos; `isinstance(str)` aceitava e sqlite
  explodia em `UnicodeEncodeError`. `_eval_str` valida encodabilidade
  UTF-8 em `trace_ids[]`/`project_id`/`dataset`. `get_trace` também
  mapeia `UnicodeEncodeError → None` (argv surrogateescape no CLI
  trilha o mesmo caminho).
- **Ordem de validação**: `_eval_specs` (barato) antes de
  `_eval_traces` (fan-out de lookups) — spec inválido não custa 500
  queries antes do 422.
- **`project_id` por side documentado** em api.md + docstring —
  comparação cross-project explícita, não silenciosa.
- **Comentários honestos**: flag binary = fração de pass por trace
  (não "score∈{0,1}"); `summary` documenta `sum`.

Testes: +2 (surrogates em trace_ids/project_id/side → 422;
Infinity/-Infinity/NaN/1e999 → score=None, nunca veredito fabricado).

## Round 16 — store idempotência + limites de SDK

- **Status idempotente (W1)**: o INSERT bindava o `trace.status`
  declarado enquanto o UPDATE recomputava via CASE sobre spans —
  um trace `status=ERROR` com spans OK flipava error→ok na 2ª escrita,
  e `status=OK` com span error ficava invisível para
  `list_traces(status="error")` na 1ª. O INSERT agora binda
  `_derived_status_code(spans)` — a mesma fórmula do CASE — então
  escrita #1 e re-delivery idêntico sempre concordam.
- **Naive datetimes em `upsert_trace` direto (W2)**: `SpanEvent.time`
  naive round-trippava naive → `replay_timeline` TypeError→500. O
  serializador coage `ensure_aware` — o store é a fronteira final,
  não só o pipeline.
- **Floats não-finitos persistiam JSON inválido (W3)**: `NaN`/`Infinity`
  viram literais nus (`json_valid()=0`) e `json_patch` mutava
  silenciosamente para null. `_json_finite` coage → `None` em
  attributes/events/links/resource_attributes antes do dumps — o
  "não representável" honesto.
- **`dead_letters` bounded (W4)**: cap `max_dead_letters` (default
  1000) + `dead_letters_dropped` counter — exporter permanentemente
  quebrado não cresce memória; callback ainda vê todas. TS SDK idem
  (`maxDeadLetters`/`deadLettersDropped`).
- **`retry_amplification` sob `capture=metadata`**: `tool_call` emite
  `forge_lens.tool_args_sha256` sempre que `arguments` é passado —
  fingerprint metadata-safe; args nunca deixam o cliente.
- **`ModelPrice` rejeita rates não-finitos** (`__post_init__` isfinite).
- **`duration_ms` negativo (clock skew) clampeado a 0** em
  critical_path/self_time.

Testes: +5 (status idempotente nas 2 direções, naive event→replay,
NaN→null+json_valid, dead-letter cap+overflow, fingerprint metadata).

## Round 17 — `tool_call` nunca quebra o host

- `json.dumps(arguments, sort_keys=True)` levanta `TypeError` em
  dicts com chaves de tipos mistos; a fingerprint escapava para a
  aplicação observada — violando o invariante "observability must
  never change the observed program's behavior" (o mesmo módulo
  engole export/redact/callback errors por essa razão).
- Sha path: `TypeError`/`ValueError` → `forge_lens.tool_args_unhashable`
  marker (evidência, não silêncio); params path: mesmo tratamento →
  `{"_forge_lens.unserializable": true}` parseável, no molde do
  marcador de truncamento existente.

Testes: +1 (mixed/tuple keys × capture_args — host nunca recebe exceção,
marcadores corretos por caminho).

## Round 18 — reconciliação de docs + cleanups do reviewer

Docs escritas nos ciclos 00/01 ainda afirmavam que trabalho já
implementado não existia (P1 no próprio taxonomia do projeto — claims
devem casar com evidência). Reconciliadas com numeração canônica do
roadmap (Studio=02, graph=03, cost=04, investigação=05, eval=06,
MCP/hosts=10):

- `overview.md`: diagrama de componentes agora inclui a camada
  query/intel implementada; lista roadmap usa números canônicos e
  separa "implementado" (03–06) de roadmap real (02, 07–12).
- `graph.md`: `DOCUMENTED_ONLY` → descrição do motor real em
  `graph/` (EdgeKind parent/sequence/link, critical_path cycle-safe,
  self_time, cycle_member_ids, endpoints `/v1/graph/*`); arestas
  semânticas e BlobRef ficam explicitamente futuras.
- `graph-explorer.md`/`trace-explorer.md`: backend existe (ciclo 03);
  a UI interativa é ciclo 02 `NOT_STARTED`.
- `evaluations.md`: `DOCUMENTED_ONLY` → backend ciclo 06 real
  (evaluators, endpoints, score=None, z-test, bounds); datasets
  persistidos/CI gates/evaluators custom ficam como roadmap.
- `replay.md`: observed replay implementado (ciclo 05);
  simulação/re-execução seguem roadmap (spec do 05, não implementado).
- `cost-analysis.md`: registry de preços, `/cost`, `/pricing` e waste
  detection documentados como implementados (ciclo 04).
- `mcp.md`/`integrations.md`/`competitive-analysis.md`/`threat-model.md`:
  números de ciclo corrigidos para o mapa canônico.
- `cycle-01-final-report.md`: rodapé de limitações reescrito com
  numeração canônica + ponteiro para status atual.
- **`cycle-02-final-report.md` criado**: `NOT_STARTED`, tabela do spec,
  por que ficou para trás, e as APIs backend prontas para a UI.

Cleanups de código (não-bloqueantes do review):

- `pipeline.py`: agregação de `SanitizeReport` itera
  `dataclasses.fields()` — um contador futuro não pode ser dropado
  silenciosamente por lista de nomes stale.
- `evaluators._token_budget`: `bool`/`str` rejeitados explicitamente
  (`True`→budget 1 e `"500"` silencioso eram arestas sloppies);
  `score=None` + reason, como todo param inválido.
- `cycle-00-final-report.md`: "Pydantic v1" → "Pydantic 2
  (`pydantic>=2.7`) com `schema_version` v1" (era ambíguo contra a
  dependência real).
- `jsonschema.py`: comentário explicando `parents[3]` (assume checkout
  do repo — correto em CI/dev, errado se instalado).
- Dirs scaffold órfãos removidos: `api/routes/`, `ingestion/`,
  `security/` (vazios, não trackeados).

Testes: +2 (`_token_budget` rejeita bool/str → `score=None`;
agregação sanitize cobre todos os fields do dataclass).

## Round 19 — veredito falso no evaluator + cobertura de credenciais

- **`token_budget` ignorava `total_tokens`-only usage**: um span
  reportando só o contador agregado somava 0 no budget — trace com
  `total_tokens=50000` passava `max_total_tokens=1000`. Corrigido para a
  convenção de `trace_cost` (`total_tokens or input+output`); o veredito
  falso de "improvement proven" não é mais emitível.
- **Denylist de credenciais alargada**: `passphrase`, `cvv`/`cvc`
  (+variantes), `pin`, `otp`, `card_number`, `security_code`, a família
  `*[-_.]key` (signing/ssh/session/hmac/encryption/master) e pares
  `(access|refresh|id)[-_.]tokens` — tudo ancorado em separadores, então
  `monkey`/`spinner`/`shipping`/`keepin` e contadores `*_tokens` (métricas,
  não segredos) seguem intactos.
- **`_finalize` marcava traces vazias `ok`**: `all([])` é True — uma
  trace sem spans não observou nada; fica `unset` (paridade com o SDK TS).
- **`InMemoryExporter` bounded em ambos SDKs**: default do tracer não
  pode crescer para sempre — `max_traces`/`maxTraces=1000` + contador
  `traces_dropped`/`tracesDropped` (mesma convenção dos dead-letters).
- **`eval/compare` valida as duas sides antes de fan-out**: `b` malformada
  → 422 sobre `b`, nunca mascarada pelo lookup 404 de `a` (split
  `_eval_trace_ids` shape-only vs resolução).
- Defensivos: sort key de `_children` agora uniforme `str()` (datetime
  nunca encontra str numa comparação em grafos hand-built); `ORDER BY
  started_ns, span_id` (tiebreak determinístico); `AGENTS.md` documenta a
  exceção de clock (perf_counter em self-metrics + clock injetável do
  RateLimiter); chaves não-str de atributos normalizadas via `str()` em
  `sanitize_attributes`/`_sanitize_value` com contador `normalized_keys`
  (evidência, nunca silêncio — e flui pela agregação field-driven do r18).

Testes: +8 (total-only budget verdict, denylist sensível, falsos
positivos limpos, normalized_keys, empty-trace unset, InMemory bound Py,
compare ordering 422, InMemory bound TS).

## Round 20 — correção dos próprios fixes do r19

O reviewer encontrou dois defeitos introduzidos no round-19:

- **`traces_dropped`/`tracesDropped` ficava negativo em export normal**:
  `dropped = len(traces) - room` com `room > len(traces)` → -997 num
  export de 3 traces no exporter *default* — telemetria fabricada na
  configuração default, a classe P0 que o projeto existe para policiar.
  Corrigido com `kept = traces[:room]` + `dropped = len - len(kept)` em
  ambos SDKs; o contador agora permanece exatamente 0 sob capacidade.
  Resposta de `export()` também expõe `traces_dropped` por chamada
  (paridade com a convenção de ingest-result), e `max_traces`/`maxTraces`
  é clampado a ≥0.
- **Ancoragem da denylist não cumpria o anunciado**:
  - `(access|refresh|id)[-_.]tokens$` tinha alternativas sem âncora
    esquerda — `valid_tokens`/`invalid_tokens`/`paid_tokens`/
    `xaccess_tokens` casavam via substring. Agora `(^|[-_.])(access|
    refresh|id)[-_.]tokens$`.
  - `[-_.]key$` era amplo demais — comia atributos OTel semconv reais
    (`messaging.kafka.message.key`, `routing_key`, `partition_key`,
    `sort_key`, `idempotency_key`…). Substituído por enumeração de
    nomes credential-shaped: signing/ssh/session/hmac/encryption/
    decryption/master/license/auth/client/pgp/gpg/tls/ssl/service-account.
  - `(^|[-_.])(pin|otp|cvv)([-_.]|$)` redigia contadores (`pin_count`,
    `otp_attempts`, `pin_retries`) — agora pin/otp só casam sozinhos ou
    com sufixo credential explícito (code|number|value|hash|secret);
    cvv/cvc só no fim com dígito opcional.
  - `card[-_.]?number`/`security[-_.]?code` ancorados nos dois lados —
    `discard_number`, `wildcard.number`, `card_number_count`,
    `security_code_review` ficam limpos.
- `token_budget` volta a `total_tokens or (input+output)` — idêntico a
  `trace_cost` (`or` em vez de `is not None`: `total=0` contraditório com
  parts≠0 conta as parts; super-contar é a direção segura para budgets).

Testes: a lista de falsos-positivos cobre todos os contraexemplos do
reviewer (metric `*_tokens` lookalikes, OTel `*key`, pin/otp counters,
card/security lookalikes); os dois SDKs testam `dropped == 0` sob
capacidade.

## Round 21 — accepted≠retained, stale ok, camelCase tokens (revisão cadeec4)

- **`InMemoryExporter.export` não pode mais fabricar aceite** (os dois
  SDKs): `accepted_spans`/`traces` passam a cobrir apenas `kept` —
  convenção `IngestResult.accepted_spans` (aceite = retido). O payload
  ganha `submitted_traces`; `traces_dropped` segue separado. Docstring
  documenta a diferença proposital de shape vs `HTTPExporter` (que
  devolve a resposta do servidor — aceite do servidor, não retenção).
- **`_finalize` reseta status** (Py SDK): `else → StatusCode.UNSET`, o
  terceiro ramo que o TS já tinha — flush mid-flight após um all-ok
  seguido de span unset não carrega mais um `ok` obsoleto no payload.
- **Denylist camelCase plural**: `(access|refresh|id)[-_.]?tokens$` —
  `accessTokens`/`refreshTokens`/`idTokens` redigem; a âncora esquerda
  mantém `validTokens`/`paidTokens`/`xaccessTokens` limpos. Gaps
  residuais fechados na mesma classe: `otp_number`/`otp.number`,
  `cvv22`/`cvc123` (`[0-9]*`), `card_no`, `social_security[_number]`,
  `aws_key`/`kms_key` + plural `*keys` na enumeração (`routing_keys`,
  `primary_keys`, `monkeys`, `xaws.key`, `awaken` limpos).
- **`max_traces`/`maxTraces` coerção**: `int()`/`Math.trunc` — `1.5`
  não derruba mais o slice (paridade com o slice tolerante do TS).
- **`_children` ordena por instante**: chave `(missing, unix_nano, id)`
  — `+12:00` vs `Z` ordenam pelo instante real, não pela string local;
  targets dangling ordenam por id, sem comparação de tipos mistos.
- **`list_traces` tiebreak**: `ORDER BY started_ns DESC, trace_id` —
  paginação determinística sob instantes iguais (mesma classe do fix
  `started_ns, span_id` do round 19).

Testes: payload kept-only nos dois SDKs (`max=1`: 1 id aceito, 1
dropped, `submitted=2`); re-finalize `ok → unset`; `dropped == 0` sob
capacidade preservado; denylist com todos os novos padrões +
lookalikes camelCase/cardano/awaken/monkeys; `_children` instant-order;
`list_traces` com mesmo `started_ns`.

## Round 22 — eval boundary UnicodeDecodeError; dropped_attributes (revisão 7539f0b)

- **`/v1/eval/run|compare` 500 → 400**: `json.loads(bytes)` levanta
  `UnicodeDecodeError` em UTF-8 inválido — classe distinta de
  `JSONDecodeError`. `_eval_body` agora captura ambos (a ingestão OTLP
  já usava `except Exception` → 400). 4 payloads × 2 endpoints pinados
  com `== 400` explícito.
- **`_attrs` não descarta mais silenciosamente**: itens top-level que
  não são `{"key": str, ...}` contam em `_otlp.dropped_attributes` —
  mesmo contrato de evidência de `dropped_kv_pairs`/`events_dropped`/
  `links_dropped`; docstring do módulo atualizada.
- **`serve --port 0`**: `port or ep_port or 7777` comia o zero
  (ephemeral bind) — comparações `is not None` nos dois níveis.
- **quickstart.py**: docstring apontava `./.forge-lens/lens.db`; o
  código escreve em `$TMPDIR/forge-lens-quickstart/` — corrigido com
  o `--db` explícito nos comandos de inspeção.
- **README status**: "remaining cycles DOCUMENTED_ONLY" era generoso
  demais — 07–12 não têm docs. Agora: ciclo 02 `NOT_STARTED`, 07–12
  roadmap.

## Round 23 — RecursionError no boundary; denylist simétrico (revisão 999a9da)

- **`_eval_body` RecursionError → 400**: JSON válido com 80k níveis de
  aninhamento levantava `RecursionError` → 500 nas duas rotas `/v1/eval/*`
  (a OTLP já devolvia 400). Adicionado à tupla explícita — NÃO `except
  Exception`: o `HTTPException` 413 de `_read_capped_body` vive dentro do
  mesmo `try` e precisa propagar intacto.
- **`eval_run` valida `dataset` antes do fan-out**: `_eval_dataset` era
  avaliado depois de `_eval_traces` (ordem de argumentos) — `dataset:123`
  + trace inexistente devolvia 404 em vez de 422. Mesmo princípio do
  round-19 agora aplicado nos dois endpoints.
- **Denylist sem assimetrias internas**:
  - pin/otp com separador opcional antes do sufixo credential +
    prefixos (`card|user|login|bank|account`): `pinCode`, `otpValue`,
    `cardPin`, `user.pinCode` redigem; `pincode` (CEP) é colisão aceita
    — direção credential-safe documentada no regex.
  - plural tokens: `session|auth|api|bearer|csrf|jwt|oauth` adicionados
    — `sessionTokens`/`api_tokens`/`jwt.tokens`/`oauthTokens` redigem;
    âncora esquerda mantém `rapidTokens`, `capitalTokens`, `xauthTokens`,
    `invalidSessionTokens`, `authenticTokens`, `oauth_tokens_count` limpos.
  - cvv/cvc com sufixo nomeado: `cvv_code`, `cvc_number`, `cvc_no`
    redigem; `cvv_count` segue limpo.
- **TS `maxTraces` fail-fast**: `Math.trunc(NaN)` zerava a capacidade
  silenciosamente — agora `Number(maxTraces)` (coage `"5"`→5 como o
  `int()` do Python) e `TypeError` em não-finito.
- **Docs sdk-{python,typescript}**: shape de retorno do `export()`
  documentado — accepted = retido (local) vs aceite do servidor (HTTP).

Testes: deep-nesting → `== 400` nos dois endpoints; dataset inválido →
422 antes do lookup; denylist com os novos padrões + lookalikes; TS com
NaN/Infinity/"garbage" → throw e coerção `"5"`/`1.9`.
