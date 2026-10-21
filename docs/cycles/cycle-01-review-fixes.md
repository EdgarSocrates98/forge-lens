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
