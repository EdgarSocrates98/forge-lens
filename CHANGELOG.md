# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/), SemVer.
`Unreleased` acumula mudanças até o próximo tag.

## [Unreleased]

### Added — portable installer (Forge parity)

- **`install.sh` / `install.ps1` / `install.windows.sh` / `setup.sh` /
  `setup.ps1`** — thin wrappers delegating to the vendored
  `scripts/forge_bootstrap.py` (canonical copy shared with
  forge-doctor-api/forge-doctor-data): pipeline `discover →
  resolve-python → create-venv → install-runtime → write-launcher →
  smoke-verify → register → report`, driven by the repo-local
  `forge.json` (`forge_id`, `package`, `distribution`, `cli`, `python`,
  `extras: [server]`, `mcp_command: forge-lens mcp-serve`,
  `install_command: python -m forge_lens install`).
- Instala em `~/.forge/installs/forge-lens` com launcher em
  `~/.local/bin` (ou shim `.cmd`), variáveis `FORGE_LENS_*` persistidas
  em `${XDG_CONFIG_HOME:-~/.config}/forjas/env.sh` (POSIX) ou HKCU
  (Windows); flags `--dry-run`, `--editable`, `--install-dir`,
  `--no-launcher`, `--python`, `--extras`. Nunca toca o `.venv` do
  projeto; wheel construído do checkout é o default (instalação
  independe do clone depois).
- **`forge-lens install`** — o módulo `install/lifecycle.py` (receipts
  auditáveis em `<data-dir>/install/receipt.json`) ganha superfície CLI:
  `install` (grava receipt, `--profile/--source/--file/--dry-run`),
  `install status`, `install doctor` (exit 1 em falha), `install
  uninstall` (remove só arquivos rastreados dentro do data-dir).
  `install-status` continua como atalho status+doctor. `__main__.py`
  adicionado para `python -m forge_lens`.
- Docs: `installation/standalone.md` (rotas completas + ciclo de vida),
  `linux.md`/`macos.md`/`windows.md` (rota portátil primeiro),
  `reference/cli.md` (verbos + flags + escopo de `--data-dir` por verbo),
  README quickstart. `install.ps1 --dry-run` é a forma correta — o
  script não declara `param()`, então flags seguem a convenção
  `--long-option` do argparse (não `-PascalCase`).
- `tests/unit/test_forge_bootstrap.py` (109 testes, stdlib/offline):
  redaction de credenciais em URLs, normalização PEP 503, specs de
  distribuição + extras, refusals de nome/path traversal, dry-run,
  conflito editable, aceitação de URLs, falha pip com redaction,
  build wheel/fallback, provenance, managed files, resolução de Python,
  venv reuse, launcher, manifest/receipt, variáveis `FORGE_LENS_*`,
  persistência POSIX idempotente, smoke (exit≠0 e stdout vazio),
  validação de config.

### Fixed — review hardening (round on `542c0a3`)

- **Overflow int64 → 500 eliminado**: `min_total_tokens`/`offset`/
  `min|max_duration_ms` acima de `2^63−1` (ou finitos que estouram na
  conversão ms→ns) agora são `422`/exit 2/`ValueError` — nunca um
  `OverflowError` do bind SQLite. Guarda `_check_i64` aplicada em
  todos os binds `LIMIT/OFFSET` do store (`list_traces`,
  `list_sessions`, `list_tools`, `list_experiments`,
  `list_experiment_results`, `list_audit`).
- **Filtros de dimensão viraram field-scoped**: `model`/`agent`/`host`/
  `tool` passam de `LIKE` sobre o blob JSON serializado (podia casar
  texto de chaves não relacionadas) para `json_each`/`json_extract`
  sobre o valor da chave exata — `gen_ai.*`, `source.system`,
  `service.name`, `source.host`, `host.name`.
- **`min_total_tokens=0` honesto**: exige pelo menos um span com
  observação de uso — ausência de evidência ≠ zero observado.
- **MCP `-32603` realmente opaco**: a mensagem carrega só o tipo da
  exceção (`internal tool error: ValueError`); `str(exc)` — que pode
  embutir payload/paths/segredos — não cruza mais o canal.
- **MCP lifecycle correto**: `ping` é aceito antes de `initialize`
  (permitido durante o handshake); demais métodos seguem gated.
- **Args malformados → `-32602`**: `TypeError` de validação de
  argumento e `arguments` não-objeto viram invalid-params; falhas
  internas seguem `-32603`.
- **`redact_export` robusto**: depth cap 8 (marcador
  `[DROPPED:depth-limit]` contado, nunca `RecursionError`) + valores
  sob chaves identidade (`*_id`, `actor`) pulam máscara de e-mail —
  mascarar join key quebrava correlação; credential patterns ainda
  se aplicam ao valor. `export_sanitize` divulga `dropped_content`.
- Store rejeita `min_total_tokens` negativo (`ValueError`) —
  alinhado ao 422 da API em vez de clampar silenciosamente.

### Added — improvement wave (post-review)

- `GET /v1/traces` server-side filters: `status`, `session_id`, `tool`,
  `q`, `since`, `until`, `min_duration_ms`, `max_duration_ms`
  (traces sem `ended_at` são excluídas por filtros de duração); os
  mesmos filtros em `forge-lens traces list`.
- `GET /v1/sessions`: roll-up por `session_id` (traces, spans,
  first_started_at, last_activity_at) + `lens.sessions` no MCP.
- `GET /v1/tools`: roll-up de frota por `tool.name` + `forge-lens tools`
  + `forge-lens sessions` (roll-up de sessões) — paridade CLI completa
  + `lens.sessions`/`lens.tools` MCP: `offset` pagination (walk roll-ups
    page-by-page; bad args → JSON-RPC error, loop survives)
  + `lens.tools` no MCP (paridade CLI/API/MCP).
- MCP stdio: `lens.detect_loops`, `lens.trace_efficiency`, `lens.cost`,
  `lens.failure`, `lens.list_datasets`, `lens.sessions`, `lens.tools`,
  `lens.capabilities` (11 tools read-only).
- Avaliadores `contains`, `regex`, `tool_call_count`;
  `latency_budget` aceita `max_duration_ms` (`budget_ms` segue como alias).
- Avaliadores `schema_validation` (subconjunto JSON-Schema:
  `type`/`properties`/`required`/`items`, keywords fora do subconjunto →
  `evaluator_error`), `evidence_coverage` (`required_attrs` com match
  exato/prefixo `attr.` sobre spans + resource attributes, threshold
  `min_coverage`), `human_review` (marcador de fila — sempre
  `score=None`, `verdict=neutral`, rubric ecoado), `precision_recall`
  (P/R/F1 de conjunto: modos `tool` ou `field`, contínuo → MWU no
  compare) e `claims_grounded` (heurística lexical de grounding contra
  `required_source_attr` — overlap de tokens ≥ `min_overlap`; sem fonte
  observada → `score=None`, nunca fail fabricado). Fecha os gaps de
  métodos/métricas do spec §06.2/§06.5; registry 12 → 17.
- Studio: barra de filtros completa no Trace Explorer (deep-linkável,
  saved views) e nova view `/sessions`.
- SDK Python read-side: `forge_lens.sdk.query.LensClient` (+ `QueryError`)
  — cliente fino cobrindo toda a superfície de leitura `/v1`
  (`traces`/`sessions`/`tools`, `graph/*`, `cost`, `failure`, `replay`,
  `incident`, `security`, `eval`, `efficiency`, `recommendations`,
  `loops`, datasets + versions, e os POSTs de consulta `eval/run`,
  `eval/compare`, `intel/efficiency`, `intel/regress`, `analyze/*`,
  `alerts/evaluate`, além de `health`/`metrics`/`capabilities`/
  `pricing`/`ops_summary`/`specialists`). stdlib `urllib` como o
  `HTTPExporter`; `QueryError` único com `status_code`; `from_config()`
  resolve `FORGE_LENS_*` + `.forge-lens/config.toml` como a CLI.
- Docs: `guides/operations-runbook.md`, `guides/troubleshooting.md`;
  referências `cli.md`/`mcp.md` e `security/retention.md` sincronizadas
  com o código.
- Golden-scenario corpus (`tests/golden/`): 47 cenários declarativos
  em 16 categorias sobre comportamento real (ingest/dedupe/sanitize/
  integridade/limites/tenancy/roles/roll-ups/ops/eval/datasets/
  retenção/audit/MCP/replay/grafo), um único runner com validação de
  schema do próprio corpus e doc de formato em
  `docs/reference/golden-scenarios.md`.
- Camada de benchmark smoke (`tests/bench`, marker `benchmark`):
  timings de parede reais sobre ingest via `IngestPipeline`, filtros de
  `list_traces`, roll-ups `list_sessions`/`list_tools`, `get_trace`,
  `build_trace_graph`/`build_session_graph` e bateria de eval — corpus
  sintético determinístico (1k traces semeadas via pipeline), sem
  sockets nem relógio de parede nos dados; sumário impresso ao fim da
  sessão + `--bench-report=PATH` para JSON; único teto de tempo é um
  tripwire generoso (ingest 500 traces < 30 s). Doc em
  `docs/reference/benchmarks.md`.
- CLI read-parity: `forge-lens capabilities` (mesmo payload de
  `/v1/capabilities`/`lens.capabilities`), `forge-lens stats`
  (`/v1/ops/summary`), `forge-lens datasets list|show` (metadata +
  versions), e as análises por trace `failure`/`loops`
  (`--repeat-threshold`)/`efficiency`/`recommendations`/`security`/
  `replay` espelhando os GETs `/v1/traces/{id}/…` — trace inexistente
  → exit 2, `--json` emite o contrato da rota.
- `forge-lens import-otel <file>`: importa dump
  `ExportTraceServiceRequest` JSON (paridade com
  `POST /v1/otlp/traces`) — bound `max_payload_bytes`, quarentena de
  spans malformados como warnings, `provenance=imported`, via
  `IngestPipeline`.
- Entidade `Experiment` persistida (audit gap 06.3): contrato
  `contracts/v1/experiment.py` + `schemas/v1/experiment.json`, tabelas
  `experiments`/`experiment_results` (runs append-only) e rotas
  `PUT /v1/experiments/{id}`, `GET /v1/experiments` (envelope),
  `GET /v1/experiments/{id}` (com `runs` paginado) e
  `POST /v1/experiments/{id}/run` — executa o mesmo caminho de
  `/v1/eval/compare` sobre os cohorts declarados, grava o report e
  avança `status: open→completed`; escopo por projeto como datasets.
  Doc: `docs/reference/experiments.md`.
- `eval/compare`: intervalos de Wilson (95%) em `ci_a`/`ci_b` para
  métricas binárias, Mann-Whitney U (aprox. normal + correções de
  continuidade/empates, n≥10/lado) para métricas contínuas — campo
  `test` nomeia o teste, `not_tested` agora significa avaliador fora do
  registry — e nota `multiple_comparisons` quando >1 teste roda (α=0.05
  sem correção); `fail_on_significant_regression` passa a alcançar
  regressões contínuas significativas.
- §05.5 correlation (`forge_lens.intel.correlation`):
  `correlate_trace(trace)` pareia sinais dentro de uma execução —
  retries que precederam erro, cluster de falhas de tool, troca de
  modelo, crescimento de contexto antes de erro/score de eval, erros
  antes de score — cada associação com `evidence_span_ids`, `strength`
  (números reais) e `caveat`, rotulada `temporal_association` (nunca
  causal); `correlate_fleet(store, project_id, *, limit=200)` monta
  tabelas de co-ocorrência sobre janela ≤500 (tool↔falha,
  modelo↔falha, tamanho-de-sessão↔falha, retries↔score de eval
  observado) com `n` por cohort e `insufficient_data` honesto sob os
  pisos (≥5/cohort, ≥3 scored/bucket). Doc:
  `docs/reference/correlation.md`; testes em
  `tests/unit/test_correlation.py`.
- §07.5 métricas derivadas de alerta (`forge_lens.ops.alerts`):
  `resolve_metrics(store, project_id, registry, *, window, only)` —
  cada nome em `METRICS` tem resolver próprio. Contadores seguem
  idênticos sobre `store.stats` (corpus do projeto: `span_count`,
  `trace_count`, `error_count`, `error_rate`); os novos resolvem sobre
  a janela limitada das ≤`METRIC_WINDOW` (200) traces mais recentes:
  `avg_duration_ms`, `max_duration_ms`, `p95_duration_ms`
  (nearest-rank — o store não calcula percentis; trace rodando é
  excluída), `error_traces`/`error_trace_rate` (nível de trace,
  distinto de spans), `token_total` (mesmo fallback de `trace_cost`),
  `cost_total_usd`/`cost_per_trace_usd` (uso de modelo sem preço é
  excluído, não zerado), `unpriced_model_count` (gasto cego) e
  `loop_traces` (`detect_loops` bound na janela). Sem
  `PricingRegistry` as métricas de custo são omitidas, nunca
  fabricadas como zero. `eval_pass_rate` deliberadamente ausente:
  resultados de eval são efêmeros e reports de experimento são deltas
  por experiment/cohort/evaluator — escalar por projeto não resolvível
  honestamente. `/v1/alerts/evaluate` passa `only` com as métricas das
  regras — avaliação só de contadores não escaneia traces. Tabela de
  unidades/bounds em `docs/reference/api.md`; 12 testes em
  `tests/unit/test_alert_metrics.py`.
- §05.3 taxonomia completa de falha (`forge_lens.investigate.classify`):
  7 categorias novas detectadas só por evidência registrada —
  `invalid_arguments` (400/422, `ValidationError`/`TypeError`,
  "invalid/missing argument"), `schema_mismatch` (schema/structured-
  output/parse — antes de invalid_args por especificidade),
  `auth_error` (401, `*Auth*`/`Unauthorized` types, credencial
  inválida/expirada), `missing_permissions` (403, `PermissionError`,
  forbidden/denied), `retry_exhaustion` (counters `retry.*`/`attempt*`
  ≥ max registrado ou ≥3, `retry` events ×3, `RetryError`), 
  `agent_cancelled` (`CancelledError`/flags `cancelled` — sinal forte em
  span não-errored também falha a trace; "order cancelled" em span
  saudável não conta) e `budget_exceeded` (flags `*.budget.exceeded`,
  termos quota/budget — um 429 nomeando quota é exaustão de conta,
  ganha de `rate_limit`). `evidence` agora carrega `signals`
  (labels por span) + `names`; chaves legadas (`status`, `message`)
  preservadas.
- §05.4 `Hypothesis` + `build_hypotheses(trace)` em
  `forge_lens.investigate.hypothesis`: `{hypothesis_id, label, symptom,
  supporting_evidence, contradictory_evidence, missing_evidence,
  suggested_verification, confidence, status}` — uma hipótese por
  categoria sinalizada (primária + concorrentes), `status` honesto
  (`proposed`/`supported`/`refuted` só com evidência contraditória/
  `unverifiable` para falha sem assinatura), bounded por
  `MAX_HYPOTHESES`.
- §12.7 `suggest_evaluators(reports)` em
  `forge_lens.investigate.eval_suggest`: mapeia categorias (single ou
  recorrentes entre traces, `recurring` ≥2) para specs candidatas de
  avaliador/dataset-case com `rationale` + `evidence` — ex.:
  `invalid_arguments` → `allowed_tools`/`tool_call_count` guard +
  dataset case, `timeout` → `latency_budget`. Datasets nunca são
  modificados.
- `failure_investigator` embute nos findings `hypotheses` (cap
  `context.max_hypotheses`, default 5, com `hypothesis_count`/
  `hypotheses_truncated`) + `eval_suggestions`; o bundle `/incident`
  ganha as mesmas seções. Doc: `docs/guides/failure-investigation.md`;
  23 testes em `tests/unit/test_failure_categories.py`.

### Added — docs & community (post-review wave)

- `.github/ISSUE_TEMPLATE/`: `bug_report.md` e `feature_request.md` (com
  checklist honesto) + `config.yml` roteando vulnerabilidades para
  Security Advisories e dúvidas para `docs/`.
- `docs/roadmap.md` — roadmap público: resumo da superfície entregue e
  tabela **Declared deferrals** convertendo todo deferral conhecido em
  descope explícito com racional e gatilho de revisita (encryption-at-
  rest, scalable mode, browser matrix, 1M spans, SSO/OIDC, re-execution
  replay, canais de alerta, MCP ao vivo, adapters restantes, §12.5–12.8
  residuais, OTLP/protobuf, filtros/virtual-scroll do Studio, eval CI
  gate) + itens PLANNED priorizados por valor medido.
- `docs/cycles/cycle-07..12-final-report.md` — relatórios individuais
  por ciclo (escopo/verificável, semântica honesta, exclusões,
  evidência) conforme o layout mandatório da spec §9; `cycle-06-12.md`
  segue como índice consolidado com nota apontando para eles.
- `docs/adoption.md` — matriz de valor real (§12.10): quem se
  beneficia, o que custa, o que NÃO faz ainda (link p/ roadmap) e como
  feedback vira prioridade.
- `examples/sample-otlp.json` — dump `ExportTraceServiceRequest` mínimo
  (agente → llm + tool) pronto para `forge-lens import-otel`.

### Added — integration hardening (post-review wave)

- §02.6 — `list_traces` ganha `model`/`agent`/`host`/`min_total_tokens`
  em todas as superfícies: store (`sqlite.py`), `GET /v1/traces`, CLI
  `--model/--agent/--host/--min-tokens`, SDK Python
  (`list_traces(model=…)`), SDK TS (`listTraces({model, agent, host,
  minTotalTokens})`). Deliberado: sem filtro `cost` — custo deriva do
  pricing na leitura, nunca predicado persistido.
- §05.5 roteado: `GET /v1/traces/{id}/correlate` (co-ocorrências
  temporais por trace) + `POST /v1/intel/correlate` (tabelas de frota,
  janela ≤500) — classificações `temporal_association`/
  `descriptive_co_occurrence`, zero claim causal. Paridade: CLI
  `forge-lens correlate`, MCP `lens.correlate`, SDKs
  `trace_correlate`/`fleet_correlate` + `traceCorrelate`/
  `fleetCorrelate`.
- MCP hardening: `tools/call` e demais requests exigem `initialize`
  (pré-init → `-32602`; `ping` permanece permitido durante o
  handshake), falhas de dados pós-init viram `result.isError`,
  exceções viram `-32603` com mensagem estática + tipo — nenhum
  `str(exc)` no canal.
- `/v1/traces/{id}/incident`: export-boundary re-sanitize
  (`redact_export`) — findings derivados não podem ecoar segredos
  capturados sob `capture=full`; resposta ganha `export_sanitize`
  `{redacted_keys, redacted_values, emails_masked, dropped_content}`.
  Regressão em
  `test_security.py`.
- Studio: replay playback controls (step/play/pause/velocidade/jump-to
  error·tool·event + teclado; `Date.parse` por row por CD →
  pré-computado), rota `/experiments` (list/create/run/gate verdict),
  `compare-report` compartilhado com failure examples recomputados e
  Pareto quando há means reais, mirror de avaliadores 12 → 17.
- Docs: `api.md` (novos filtros + rotas correlate + `export_sanitize`),
  `cli.md` (novas flags + `correlate`), `mcp.md` (init gate +
  `lens.correlate`), `sdk-python.md`/`sdk-typescript.md` (filtros +
  métodos), `architecture/security.md` e `security/threat-model.md`
  (drift corrigido: TLS passthrough, keys/RBAC/audit do ciclo 08).

### Added — cycles 06–12

- Datasets versionados + casos (ground truth), avaliadores
  `exact_match`/`token_f1`/`allowed_tools`/`task_completion`, gate de
  regressão em `/v1/eval/compare`.
- Auth modes `none|key|keys`: API keys com hash, roles
  reader/writer/admin, escopo por projeto, isolamento de tenants,
  endpoints `/v1/admin/{keys,audit,prune}`, audit trail sem segredos,
  `forge-lens prune|archive`, TLS em `serve`.
- Loop detection (`/v1/traces/{id}/loops`), runtime de especialistas
  com budget/cancelamento (`/v1/analyze/*`), alertas por threshold
  (`/v1/alerts/evaluate`), dashboard ops (`/v1/ops/summary`).
- SSE replay `/v1/traces/{id}/stream`, benchmark real medido
  (scripts/benchmark.py → docs/benchmarks).
- Inteligência avançada: `/v1/intel/{efficiency,regress}`,
  `/v1/traces/{id}/{efficiency,recommendations}`,
  `/v1/graph/traces/{id}/analysis`.
- Adaptadores: matriz honesta de capacidades (`/v1/capabilities`),
  importer Claude Code JSONL (`import-claude`), servidor MCP stdio
  (`mcp-serve`), lifecycle de instalação com receipts
  (`install-status`), 7 skills em `skills/`.
- Release engineering: `scripts/gen_sbom.py` (CycloneDX do ambiente
  instalado), workflow release.yml com OIDC + gate de versão.

## [0.1.0] — baseline

- Cycles 00–05: contratos, ingest OTLP/nativo, store SQLite WAL,
  sanitização, SDKs Py/TS, CLI, grafo, custo/tokens, replay/incidente,
  eval determinístico. 270+ testes, ruff/mypy-strict limpos.
