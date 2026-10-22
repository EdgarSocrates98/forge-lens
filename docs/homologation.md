# Homologação operacional — cenários A-K

Execução ponta-a-ponta contra a superfície instalada (binário `forge-lens`
do `.venv`, API `forge-lens serve` em subprocess, MCP por stdio). Regenerado por `homolog/run_scenarios.py`.

| Cenário | Objetivo | Status | Passos | Evidências |
|---|---|---|---|---|
| A — Deploy local: init → doctor → serve → inspect | Subir a stack local do zero e inspecionar saúde/capacidades. | **PASS** | 8/8 | `homolog/evidence/A/01-init.txt`; `homolog/evidence/A/02-doctor.txt`; `homolog/evidence/A/03-capabilities.txt`; `homolog/evidence/A/04-health.json`; `homolog/evidence/A/05-ops-summary.json`; `homolog/evidence/A/06-capabilities.json` |
| B — Ingest real → traces → detail → cost → graph → MCP | Traços persistidos consultáveis via REST e via MCP stdio. | **PASS** | 10/10 | `homolog/evidence/B/01-traces.json`; `homolog/evidence/B/02-trace-detail.json`; `homolog/evidence/B/03-cost.json`; `homolog/evidence/B/04-graph.json` |
| C — Investigação: failure → replay → incident bundle | Investigar um traço com falha e exportar o bundle de evidências. | **PASS** | 7/7 | `homolog/evidence/C/01-failure.json`; `homolog/evidence/C/02-replay.json`; `homolog/evidence/C/03-incident.json`; `homolog/evidence/C/04-cli-failure.txt`; `homolog/evidence/C/05-cli-replay.txt` |
| D — Graph em escala (≥1000 spans) | Construir e consultar o grafo de um traço com ≥1000 spans. | **PASS** | 5/5 | `homolog/evidence/D/01-graph-1200.json`; `homolog/evidence/D/02-graph-analysis.json`; `homolog/evidence/D/03-critical-path.json` |
| E — Eval: baseline vs candidato → compare → gate | Rodar avaliadores em coortes baseline/candidato e comparar com stats. | **PASS** | 5/5 | `homolog/evidence/E/01-eval-run.json`; `homolog/evidence/E/02-eval-compare.json`; `homolog/evidence/E/03-cli-eval.txt` |
| F — Sanitização de segredos em todas as leituras | Garantir que credenciais em spans nunca saem por nenhuma superfície. | **PASS** | 6/6 | `homolog/evidence/F/00-ingest-response.json`; `homolog/evidence/F/01-api-trace.json`; `homolog/evidence/F/02-api-incident.json`; `homolog/evidence/F/03-cli-show.txt`; `homolog/evidence/F/04-mcp-trace.json` |
| G — Export/pack de evidências | Exportar o pacote de evidências (incident bundle) e validar conteúdo. | **PASS** | 4/4 | `homolog/evidence/G/01-incident-bundle.json`; `homolog/evidence/G/02-cli-incident.txt` |
| H — Retenção: prune + archive | Aplicar retenção: arquivar e apagar traços antigos com auditoria. | **PASS** | 5/5 | `homolog/evidence/H/01-archive.txt`; `homolog/evidence/H/02-prune.txt`; `homolog/evidence/H/03-traces-list.txt` |
| I — Backup e restauração | Copiar a store (WAL checkpoint + cópia) e reabrir com conteúdo íntegro. | **PASS** | 4/4 | `homolog/evidence/I/01-archive-all.txt` |
| J — Operação 100% offline | Provar que toda a superfície local opera sem rede (proxies mortos). | **PASS** | 4/4 | `homolog/evidence/J/01-init-offline.txt` |
| K — Autorização: permissões inadequadas rejeitadas | Exercitar auth_mode=keys num serve real: key ausente/errada, role insuficiente e escopo de projeto errado são rejeitados. | **PASS** | 9/9 | `homolog/evidence/K/01-post-no-key.json`; `homolog/evidence/K/02-post-bad-key.json`; `homolog/evidence/K/03-post-reader.json`; `homolog/evidence/K/04-post-out-of-scope.json`; `homolog/evidence/K/05-post-writer-ok.json`; `homolog/evidence/K/06-get-no-key.json`; `homolog/evidence/K/07-get-reader.json` |

## Detalhes por cenário

### A — Deploy local: init → doctor → serve → inspect

- **Objetivo:** Subir a stack local do zero e inspecionar saúde/capacidades.
- **Esperado:** init cria .forge-lens; doctor saudável; serve responde /v1/health, /v1/ops/summary e /v1/capabilities.
- **Status:** PASS

- ✅ `init` — rc=0 — evidência: `homolog/evidence/A/01-init.txt`
- ✅ `seed` — trace de exemplo persistido
- ✅ `doctor` — rc=0 — evidência: `homolog/evidence/A/02-doctor.txt`
- ✅ `capabilities (CLI)` — rc=0 — evidência: `homolog/evidence/A/03-capabilities.txt`
- ✅ `serve` — http://127.0.0.1:55195
- ✅ `GET /v1/health` — HTTP 200 — evidência: `homolog/evidence/A/04-health.json`
- ✅ `GET /v1/ops/summary` — HTTP 200 — evidência: `homolog/evidence/A/05-ops-summary.json`
- ✅ `GET /v1/capabilities` — HTTP 200 — evidência: `homolog/evidence/A/06-capabilities.json`

  _Nota:_ Studio (apps/studio) é servido separadamente (ng serve/build) — homologado na suíte E2E Playwright, ver docs/studio-e2e.md.

### B — Ingest real → traces → detail → cost → graph → MCP

- **Objetivo:** Traços persistidos consultáveis via REST e via MCP stdio.
- **Esperado:** Traço visível em /v1/traces, detalhe completo, /cost deriva custo, /graph/traces monta a árvore, MCP lens.list_traces + lens.get_trace respondem.
- **Status:** PASS

- ✅ `seed trace` — bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
- ✅ `serve` — http://127.0.0.1:38919
- ✅ `GET /v1/traces` — HTTP 200 — evidência: `homolog/evidence/B/01-traces.json`
- ✅ `GET /v1/traces/{id}` — HTTP 200 — evidência: `homolog/evidence/B/02-trace-detail.json`
- ✅ `GET /v1/traces/{id}/cost` — HTTP 200 — evidência: `homolog/evidence/B/03-cost.json`
- ✅ `GET /v1/graph/traces/{id}` — HTTP 200 — evidência: `homolog/evidence/B/04-graph.json`
- ✅ `MCP initialize` — {"protocolVersion": "2024-11-05", "capabilities": {"tools": {"listChanged": false}}, "serverInfo": {"name": "forge-lens", "version": "0.1.0"}}
- ✅ `MCP tools/list` — 12 tools
- ✅ `MCP lens.list_traces`
- ✅ `MCP lens.get_trace` — bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb

  _Nota:_ Frames MCP completos em homolog/evidence/B/05-mcp-session.json

### C — Investigação: failure → replay → incident bundle

- **Objetivo:** Investigar um traço com falha e exportar o bundle de evidências.
- **Esperado:** failure classifica a falha; replay monta a timeline; incident gera bundle sem vazar segredos.
- **Status:** PASS

- ✅ `seed failing trace + embedded secret` — cccccccccccccccccccccccccccccccc
- ✅ `serve` — http://127.0.0.1:49473
- ✅ `GET /v1/traces/{id}/failure` — HTTP 200 — evidência: `homolog/evidence/C/01-failure.json`
- ✅ `GET /v1/traces/{id}/replay` — HTTP 200 — evidência: `homolog/evidence/C/02-replay.json`
- ✅ `GET /v1/traces/{id}/incident` — HTTP 200; segredo não vazado — evidência: `homolog/evidence/C/03-incident.json`
- ✅ `CLI failure` — rc=0 — evidência: `homolog/evidence/C/04-cli-failure.txt`
- ✅ `CLI replay` — rc=0 — evidência: `homolog/evidence/C/05-cli-replay.txt`

### D — Graph em escala (≥1000 spans)

- **Objetivo:** Construir e consultar o grafo de um traço com ≥1000 spans.
- **Esperado:** /v1/graph/traces/{id} responde com todos os nós/arestas; navegação no Studio documentada em docs/graph-scale.md.
- **Status:** PASS

- ✅ `seed 1200-span trace` — 1200 spans
- ✅ `serve` — http://127.0.0.1:49217
- ✅ `GET /v1/graph/traces/{id} (1200 spans)` — HTTP 200, 500 ms, 485479 bytes — evidência: `homolog/evidence/D/01-graph-1200.json`
- ✅ `GET .../analysis` — HTTP 200 — evidência: `homolog/evidence/D/02-graph-analysis.json`
- ✅ `GET .../critical-path` — HTTP 200 — evidência: `homolog/evidence/D/03-critical-path.json`

  _Nota:_ Render/navegação em escala (cap, busca, fit) é homologada no Studio — ver docs/graph-scale.md e os unit tests do apps/studio.

### E — Eval: baseline vs candidato → compare → gate

- **Objetivo:** Rodar avaliadores em coortes baseline/candidato e comparar com stats.
- **Esperado:** /v1/eval/run avalia; /v1/eval/compare retorna deltas + testes estatísticos; gate CI documentado (scripts/eval_gate.py).
- **Status:** PASS

- ✅ `seed baseline ok + candidato regressado` — baseline=e000 cand=e111
- ✅ `serve` — http://127.0.0.1:43753
- ✅ `POST /v1/eval/run` — HTTP 200 — evidência: `homolog/evidence/E/01-eval-run.json`
- ✅ `POST /v1/eval/compare` — HTTP 200 — evidência: `homolog/evidence/E/02-eval-compare.json`
- ✅ `CLI eval` — rc=0 — evidência: `homolog/evidence/E/03-cli-eval.txt`

  _Nota:_ Gate de CI para adoção por outros projetos: scripts/eval_gate.py + docs/eval-gate-workflow-example.yml.

### F — Sanitização de segredos em todas as leituras

- **Objetivo:** Garantir que credenciais em spans nunca saem por nenhuma superfície.
- **Esperado:** API, CLI, MCP e incident export exibem conteúdo mascarado; contadores de sanitização são declarados.
- **Status:** PASS

- ✅ `serve` — http://127.0.0.1:49525
- ✅ `POST /v1/traces (ingest com segredos)` — HTTP 200 — evidência: `homolog/evidence/F/00-ingest-response.json`
- ✅ `API GET /v1/traces/{id}` — HTTP 200; vazaram: nenhum — evidência: `homolog/evidence/F/01-api-trace.json`
- ✅ `API incident export` — HTTP 200; vazaram: nenhum — evidência: `homolog/evidence/F/02-api-incident.json`
- ✅ `CLI traces show` — rc=0; vazaram: nenhum — evidência: `homolog/evidence/F/03-cli-show.txt`
- ✅ `MCP lens.get_trace` — vazaram: nenhum — evidência: `homolog/evidence/F/04-mcp-trace.json`

### G — Export/pack de evidências

- **Objetivo:** Exportar o pacote de evidências (incident bundle) e validar conteúdo.
- **Esperado:** Bundle contém trace + classificação + timeline + evidências; JSON parseável; contadores de export_sanitize declarados.
- **Status:** PASS

- ✅ `seed failing trace` — a7777777777777777777777777777777
- ✅ `serve` — http://127.0.0.1:47883
- ✅ `GET /v1/traces/{id}/incident` — HTTP 200; chaves=['bundle_version', 'cost', 'eval_suggestions', 'export_sanitize', 'failure', 'graph', 'hypotheses', 'project_id', 'replay', 'session_id', 'status', 'trace_id', 'waste'] — evidência: `homolog/evidence/G/01-incident-bundle.json`
- ✅ `CLI incident` — rc=0 — evidência: `homolog/evidence/G/02-cli-incident.txt`

### H — Retenção: prune + archive

- **Objetivo:** Aplicar retenção: arquivar e apagar traços antigos com auditoria.
- **Esperado:** archive gera NDJSON; prune remove traços antigos; operação registrada em audit; traços novos permanecem.
- **Status:** PASS

- ✅ `seed 1 traço antigo (120d) + 1 recente`
- ✅ `CLI archive` — rc=0; arquivo=ok — evidência: `homolog/evidence/H/01-archive.txt`
- ✅ `CLI prune -y` — rc=0 — evidência: `homolog/evidence/H/02-prune.txt`
- ✅ `verificação pós-prune` — 1 traço(s) restante(s)
- ✅ `traces list pós-prune` — rc=0 — evidência: `homolog/evidence/H/03-traces-list.txt`

### I — Backup e restauração

- **Objetivo:** Copiar a store (WAL checkpoint + cópia) e reabrir com conteúdo íntegro.
- **Esperado:** DB copiado abre readonly com os mesmos traços; archive NDJSON cobre o restore lógico.
- **Status:** PASS

- ✅ `seed 2 traços`
- ✅ `WAL checkpoint + cópia física`
- ✅ `abrir backup readonly` — 2 traço(s) no backup
- ✅ `archive completo (backup lógico)` — 2 linha(s) NDJSON — evidência: `homolog/evidence/I/01-archive-all.txt`

### J — Operação 100% offline

- **Objetivo:** Provar que toda a superfície local opera sem rede (proxies mortos).
- **Esperado:** Com HTTP(S)_PROXY apontando para endereço morto e HOME isolada, init/serve/API/MCP funcionam — nenhuma chamada externa é tentada.
- **Status:** PASS

- ✅ `init offline` — rc=0 — evidência: `homolog/evidence/J/01-init-offline.txt`
- ✅ `serve offline` — http://127.0.0.1:40095
- ✅ `GET /v1/traces offline` — HTTP 200
- ✅ `MCP offline`

### K — Autorização: permissões inadequadas rejeitadas

- **Objetivo:** Exercitar auth_mode=keys num serve real: key ausente/errada, role insuficiente e escopo de projeto errado são rejeitados.
- **Esperado:** sem key → 401; reader em write → 403; writer fora do escopo → 403; writer no próprio projeto → 200; reader em read → 200.
- **Status:** PASS

- ✅ `seed 2 keys (reader + writer escopada a proj-a)`
- ✅ `serve auth_mode=keys` — http://127.0.0.1:50139
- ✅ `POST sem key → 401` — HTTP 401 — evidência: `homolog/evidence/K/01-post-no-key.json`
- ✅ `POST key inválida → 401` — HTTP 401 — evidência: `homolog/evidence/K/02-post-bad-key.json`
- ✅ `POST reader (sem role writer) → 403` — HTTP 403 — evidência: `homolog/evidence/K/03-post-reader.json`
- ✅ `POST writer escopada em proj-b → 403` — HTTP 403 — evidência: `homolog/evidence/K/04-post-out-of-scope.json`
- ✅ `POST writer em proj-a → 200` — HTTP 200 — evidência: `homolog/evidence/K/05-post-writer-ok.json`
- ✅ `GET sem key → 401 (keys cobre reads)` — HTTP 401 — evidência: `homolog/evidence/K/06-get-no-key.json`
- ✅ `GET reader em proj-a → 200` — HTTP 200 — evidência: `homolog/evidence/K/07-get-reader.json`
