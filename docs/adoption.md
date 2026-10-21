# Adoção — matriz de valor real (spec §12.10)

Esta é a matriz de valor **honesta** do Forge Lens: o que cada perfil
ganha hoje, quanto custa, e o que explicitamente **não** faz ainda
(cada "ainda não" é um deferral declarado em `docs/roadmap.md` — nunca
um gap escondido).

> Regra de leitura: `medido` = existe número publicado em
> `docs/benchmarks/`; `verificado` = coberto por teste na suíte;
> `declarado` = claim de design, ainda sem medição de usuário real.

## 1. Quem se beneficia

| Perfil | Benefício hoje | Custo de entrada |
|---|---|---|
| **Solo dev de agentes** | Loop de debug completo local: instrumenta (SDK/OTLP) → waterfall → failure/loops/cost por trace → eval determinístico. Zero dependência externa, funciona offline. | `uv add forge-lens` ou clone; SQLite local; `capture=metadata` default. |
| **Time pequeno** | Mesma superfície + `auth_mode=keys` (roles reader/writer/admin, tenancy por projeto, audit trail), TLS no `serve`, datasets compartilhados com gate de regressão. | Uma instância acessível; `keys` mode; sem IdP (SSO é deferral). |
| **Projeto OSS** | MIT, SBOM CycloneDX, contratos versionados (schemas JSON publicados), evidence-first (claims carregam `provenance`), issues com templates. | Sem telemetria por design — nada sai da máquina do usuário. |
| **Host/agent runtime** | Contratos `Trace`/`Evidence`/`EvaluationResult` v1 como fronteira; adapters só onde há superfície real (matriz `/v1/capabilities`). | SDKs Py/TS ou OTLP/HTTP JSON — sem vendor lock-in. |

## 2. Matriz de valor (dimensões da spec)

| Dimensão | O que existe hoje | Evidência | Ainda não faz |
|---|---|---|---|
| **Time to diagnose** | `failure` classifica 7 categorias com evidência; `incident` monta bundle (failure+graph+cost+waste+replay); `loops` acha repetição/no-progress com span_ids; hipóteses de causa-raiz determinísticas com ledger de evidência em entrega nesta wave. | verificado (`test_ops.py`, `test_graph_intel.py`) | Assistência por LLM explicando traces (deferral §12.6 — opt-in futuro). |
| **Cost visibility** | `/v1/traces/{id}/cost`, `intel/efficiency` (tokens/custo por sucesso, retry overhead), `ops/summary`, pricing registry local. | verificado | Custos de modelos sem pricing conhecido → `unknown` honesto, não estimativa. |
| **Tool debugging time** | Roll-up `/v1/tools` (calls/errors/avg_duration), replay observado + SSE stream, filter bar `tool=` no Studio. | verificado | Replay de re-execução (deferral — read-only por design). |
| **Evaluation setup time** | `eval/run` com 17 avaliadores determinísticos no registry, sem setup; datasets versionados via API/Studio; `Experiment` persiste runs A/B; golden corpus declarativo reutilizável. | verificado | Escrita automática em ground truth — sugestões são candidatas revisáveis (engine `eval_suggest` em entrega nesta wave). |
| **Graph navigation** | `/v1/graph/*` (trace/session), análise de centralidade/concentração/propagação, view `/graph` no Studio. | verificado | Diff temporal entre grafos (deferral §12.5); clustering/LOD para grafos gigantes. |
| **Successful integrations** | OTLP/HTTP JSON + OpenInference + traceparent; importer Claude Code real; MCP observer stdio (11 tools); 7 skills; LensClient Py/TS read-side. | verificado + medido | codex/devin/copilot = `unsupported` declarado (matriz explica por quê). |
| **Reliability** | SQLite WAL, dedupe idempotente, quarentena de spans malformados, store readonly no MCP, bounds em payload/query, zero telemetria. | verificado + medido (100k spans) | 1M spans não medido; HA/cluster fora de escopo local-first. |

## 3. Quanto custa adotar

- **Dependências**: `pydantic` + `typer` + `rich` no core; `server`
  extra adiciona FastAPI/uvicorn; TS SDK é zero-dep. Nenhum serviço
  externo (sem conta, sem cloud, sem daemon).
- **Storage**: um arquivo SQLite em `.forge-lens/` — backup é copiar o
  arquivo; `archive` exporta antes de deletar.
- **Privacidade**: `capture=metadata` redige segredos e descarta corpos
  por default; nada sai da máquina — o produto não tem canal de saída.
- **Studio**: pré-requisito só para desenvolvimento do frontend (Node);
  uso da API/CLI/SDK não exige Node.

## 4. O que NÃO faz ainda (atalhos para os deferrals)

Encryption-at-rest · Postgres/ClickHouse · browser matrix real ·
benchmark 1M · SSO/OIDC · replay de re-execução · canais de
notificação de alerta · instrumentação MCP ao vivo · adapters
codex/devin/copilot · diff temporal de grafos · análise assistida por
LLM · telemetria (mesmo consentida, ainda não existe mecanismo).

→ Racional e gatilho de revisita de cada um: `docs/roadmap.md` §2.

## 5. Como o feedback vira roadmap

1. Abra issue com o template `bug_report` ou `feature_request`
   (`.github/ISSUE_TEMPLATE/`).
2. `feature_request` pede evidência de necessidade — a dimensão desta
   matriz que sua dor toca é a evidência.
3. Prioridade = valor medido + feedback verificável (aceite §12.10);
   deferral reaberto exige evidência + ADR quando arquitetural.
4. Vulnerabilidades: **nunca** issue pública — GitHub Security
   Advisories (`SECURITY.md`).
