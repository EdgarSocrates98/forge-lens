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
