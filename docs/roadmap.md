# Roadmap público — Forge Lens

Este arquivo é **a** fonte de verdade sobre o que existe, o que foi
explicitamente descopeado e o que vem a seguir. Regra do projeto:
*undeclared gaps viram declared aqui* — se algo da spec
(`prompt_evo_inicial.md`) não está implementado e não está nesta página,
é um bug de documentação (abra issue).

> Convenção: `SHIPPED` = existe e tem teste; `DEFERRED` = descopeado com
> racional registrado (não é claim); `PLANNED` = intenção real, sem data
> prometida. Nenhum item `DEFERRED`/`PLANNED` pode ser citado como
> capacidade do produto.

## 1. O que já existe (resumo da superfície entregue)

Detalhe e evidência por ciclo em `docs/cycles/`; superfície de rotas em
`docs/reference/api.md`.

- **Ingest & store** (c00–01): contratos v1 versionados, OTLP/HTTP JSON,
  OpenInference attrs, W3C traceparent, SQLite WAL, sanitize
  `capture=metadata` por default, dedupe + integrity flags.
- **SDKs & CLI** (c01, c13): `forge_lens.sdk` (Python), `@forge-lens/sdk`
  (TS zero-dep), `LensClient`/`QueryError` read-side nos dois, CLI
  completa (`traces|sessions|tools|datasets|eval|failure|loops|…`,
  `import-otel`, `import-claude`, `serve`, `doctor`, `prune|archive`).
- **Investigação** (c03–05): grafo de execução `/v1/graph/*`,
  custo/tokens `/v1/traces/{id}/cost`, classificação de falha, replay
  observado, bundle de incidente.
- **Eval** (c06, c13): 17 avaliadores no registry determinístico
  (`EVALUATORS` em `eval/evaluators.py`), datasets versionados,
  `eval/run` + `eval/compare` (Wilson 95%, Mann-Whitney, gate de
  regressão), entidade `Experiment` persistida.
- **Observabilidade agêntica** (c07): loop/no-progress detection,
  11 especialistas determinísticos com runtime bounded, alertas por
  threshold, `/v1/ops/summary`.
- **Segurança** (c08): `auth_mode none|key|keys`, roles
  reader/writer/admin, tenancy por projeto, audit trail, retenção
  (`prune`/`archive`), TLS via `--ssl-certfile/--ssl-keyfile` no `serve`.
- **Escala** (c09, c13): índices, SSE replay `/v1/traces/{id}/stream`,
  benchmarks medidos até 100k spans + camada `tests/bench` contínua.
- **Ecossistema** (c10): matriz honesta `/v1/capabilities`, importer
  Claude Code, MCP observer stdio (11 tools read-only), install
  lifecycle com receipts, 7 skills.
- **Release** (c11): CI multi-OS (py + TS + Studio), SBOM CycloneDX,
  release workflow OIDC + gate de versão, SemVer + CHANGELOG +
  versioning policy.
- **Inteligência** (c12): eficiência por trace e frota, regressão por
  coorte (`observed_difference`, sem claim de significância além do
  teste nomeado), recomendações-candidatas, análise de grafo
  (centralidade/concentração/propagação).
- **Studio** (c02, c13): Angular — Trace Explorer com filter bar
  deep-linkável, waterfall + evidência, `/sessions`, `/datasets` +
  compare cohorts, eval panel, replay timeline, `/graph`, `/ops`,
  `/capabilities`, temas dark/light.

## 2. Deferrals declarados (descope explícito)

Cada linha é um **não-implementado conhecido** com racional — não um
gap escondido. Critério de reabertura: evidência real (medida ou
feedback via issue), nunca especulação.

| Item (ref. spec) | Estado | Racional | Quando revisitar |
|---|---|---|---|
| Encryption at rest (§08.7) | DEFERRED | Local-first assume disco do operador já protegido (FileVault/LUKS/BitLocker — FDE de SO); criptografia in-process duplicaria a camada e criaria falsa sensação de defesa contra um atacante com acesso ao FS onde o processo roda. Escopo do deployer registrado em `docs/architecture/security.md`. | Modo remoto/multi-usuário real, ou threat model exigindo defesa em repouso além do SO. TLS em trânsito já existe (`serve --ssl-certfile`). |
| Scalable mode Postgres/ClickHouse (§09.3) | DEFERRED | Spec exige avaliação, não adoção. Medido: ~1.7k spans/s e queries <50 ms até 100k spans em SQLite — não há necessidade medida. Regra do projeto: novo backend exige ADR; `docs/architecture/scalability.md` registra o caminho (TraceStore protocol, 1 instância/operador + export). | Latência/throughput medidos falharem em workload real, ou deployment centralizado multi-tenant se materializar. |
| Cross-browser e2e real (§11.4 — Chromium/Firefox/WebKit) | DEFERRED | Studio hoje: unit tests vitest/jsdom + `ng build` em CI (ver `docs/versioning.md`). Matriz Playwright de browsers reais é follow-up — não reivindicada. | Primeiro bug de UI específico-de-browser, ou antes de anunciar Studio como GA. |
| Benchmark 1M spans (§09.9) | DEFERRED | Medido até 100k (`docs/benchmarks/README.md`); extrapolação linear **não é número medido** e não pode ser citada. 1M exige ambiente de benchmark apropriado (a própria spec condiciona). | Quando houver hardware/janela para a corrida real; números entram em `docs/benchmarks/`. |
| SSO/OIDC multi-usuário | DEFERRED | Fora do escopo local-first (`docs/security/threat-model.md`, resíduo aceito). `keys` mode cobre local/team com RBAC por projeto. Não existe hoje interface de auth pluggable — `auth_mode` é `none|key|keys`; introduzir IdP exige primeiro essa interface + ADR. | Deployment corporativo real exigindo IdP; o passo 1 é a interface pluggable, não o OIDC. |
| Simulation / re-execution replay (§5.1 cenário 30, spec c05) | DEFERRED | Read-only **por design**: abrir trace nunca executa nada (SECURITY.md). Re-execução exige integração com agent-runtime + política de governança (allowlist, aprovação, ambiente isolado) — proposta em `docs/guides/replay.md`. | Quando existir runtime de agente integrável e política de autorização; nunca sem `provenance` separando observado de sintético. |
| Canais de notificação de alertas | DEFERRED | Alertas são pull-model por design: regras em `.forge-lens/alert-rules.json`, avaliação sob demanda via `POST /v1/alerts/evaluate` (audited). Webhook/email/pager implicariam I/O de saída — quebra o princípio "sem saída externa por design" e exigiria opt-in explícito. | Primeiro pedido real com canal concreto; provável formato: webhook opt-in documentado, desligado por default. |
| Instrumentação de tráfego MCP ao vivo | DEFERRED (`DOCUMENTED_ONLY`) | Não existe ponto universal de interceptação MCP — claim seria falso. O que existe: observer stdio `mcp-serve` (queries read-only) + mapeamento proposto para hosts que emitam spans (`docs/reference/mcp.md`). | Quando um cliente/servidor/gateway MCP real oferecer hook de instrumentação (Cursor já expõe via MCP — ver capabilities). |
| Adapters de host restantes — codex / devin / copilot | DEFERRED | A matriz `/v1/capabilities` diz por quê: codex = formato de log instável; devin = sem superfície pública de instrumentação; copilot = superfície fechada. Adapter só quando o mecanismo for real — "unsupported" é resposta válida. | Mudança na superfície do host; cada adapter novo precisa de fixture real verificado (padrão do importer Claude Code). |
| Temporal graph comparison (§12.5, parcial) | DEFERRED | Entregue: centralidade, concentração, propagação de falha, subgrafos repetidos (`/v1/graph/traces/{id}/analysis`). Comparação temporal entre grafos não implementada. | Após coortes com `intel/regress` gerarem demanda medida por diff estrutural. |
| Recomendação de cenários de eval a partir de falhas (§12.7) | SHIPPED | Engine `investigate/eval_suggest.suggest_evaluators` + superfícies: CLI `forge-lens eval-suggest <trace_id>...` (multi-report → `evidence.recurring` alcançável), painel incident no Studio exibe `eval_suggestions`, incident bundle da API já carrega o campo. Marca `classification: candidate`, nunca escreve em ground truth (regra da spec). | Curadoria/adoção real das sugestões em workflows. |
| AI-assisted analysis — LLM opcional (§12.6) | DEFERRED | Núcleo determinístico proíbe LLM calls (AGENTS.md); especialistas de análise são heurísticas nomeadas. LLM explicador seria add-on opt-in com controles de privacidade explícitos — sem isso, violaria "dados sensíveis nunca saem sem configuração explícita". | Design de boundary opt-in + política de envio definidos; não entra no core. |
| Product analytics / telemetria consentida (§12.8) | DEFERRED | Zero telemetria por design hoje. A spec só permite analytics **consentido e privacy-respecting** — não existe mecanismo; construir um é um projeto de governança, não de código. | Somente com opt-in explícito e documentação do que sai; nunca por default. |
| OTLP/protobuf + collectors externos | DESIGN (não é deferral de feature) | Parsing = JSON apenas (415 explícito) — reduz superfície de ataque (`docs/security/threat-model.md`). Aceitar protobuf exigiria ADR + novo parser. | Demanda real de infra OTel existente; protobuf entra como extra opcional, nunca obrigatório. |
| Studio: virtual scroll CDK + filtro de custo server-side | DEFERRED | Listas usam bounded rendering (200 linhas + load-more com `offset`), não viewport virtualizado. Filtros server-side cobrem status/session/tool/q/time/duração/model/agent/host/min_total_tokens/project_id — só custo agregado exige query param novo no store. | Ao primeiro uso real com listas >10k ou pedido concreto de filtro de custo. |
| UI de experimentos / eval CI gate (c06 residuals) | SHIPPED | Backend (`Experiment` persistido + `eval/compare` com gate verdict), view `/experiments` do Studio (create/run/gate), CLI `forge-lens experiments create|list|show|run`, e gate de regressão como job real em CI (`eval-gate` job com fixtures commitadas — pass exit 0, fail exit 1 assertivo). | Datasets com uso real em PRs alimentando o gate. |

## 3. Decisões deliberadas (não confundir com gap)

- `/docs` + `/openapi.json` ficam **sem auth** mesmo em `keys` mode —
  disclosure é só de schema e tooling precisa (trade-off documentado).
- `/v1/health` exige key `reader` em `keys` mode — probes de
  load balancer carregam uma key (documentado no runbook).
- Reads abertos em `auth_mode=none` — modelo local single-user; proteger
  a porta é decisão do operador.
- Especialistas de análise são determinísticos por padrão — regra que
  LLM nunca resolve melhor (`prompt_evo_inicial.md` §07.2).

## 4. Próximos itens (PLANNED — sem data prometida)

Priorizados por valor medido (`docs/adoption.md`) e feedback em issues.
Ordem aproximada, não compromisso:

1. **Documentação EN** — spec manda PT-BR primeiro; versão EN dos
   guides/README é o débito explícito para adoção internacional.
2. **Matriz de browsers real (Playwright)** — fecha o deferral §11.4.
3. **Galeria de exemplos** — mais sample traces, tutorials e receitas
   de integração (§12.9; `examples/` hoje tem o quickstart + dump OTLP).
4. **Studio**: virtual scroll, filtros custo/modelo — depende de uso
   real medido (a view `/experiments` já está em entrega nesta wave).
5. **Export Parquet / arquivamento offline** — caminho já registrado em
   `docs/architecture/scalability.md`.
6. **Benchmark 1M** em ambiente apropriado → `docs/benchmarks/`.
7. **Eval CI gate** — `eval/compare` verdict virando check de PR.
8. **Alert delivery opt-in** (webhook) — somente com demanda real.
9. **Novos host adapters** quando a superfície existir (codex é o mais
   provável — logs já existem, formato instável é o bloqueio).
10. **Diff temporal de grafos** (§12.5 residual).

## 5. Como priorizamos

- Bug/evidência em issue pesa mais que pedido abstrato.
- Medição > projeção (regra §5.2 de benchmarks aplica a roadmap).
- Feedback entra pela matriz de valor em `docs/adoption.md` — abra uma
  issue `enhancement` citando a dimensão afetada.
- Deferral reaberto exige o mesmo rigor do descope: evidência + ADR
  quando arquitetural.
