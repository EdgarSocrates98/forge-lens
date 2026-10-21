# Market Landscape — Observabilidade Agêntica

> Ciclo 00. Escopo: plataformas de tracing/observabilidade para agentes e LLMs.
> Data da análise: outubro/2026 (conhecimento de domínio + docs públicos).

## Categorias

### 1. Plataformas LLM-observability dedicadas

| Produto | Modelo | Fortes | Lacunas relevantes p/ Forge Lens |
|---|---|---|---|
| **Langfuse** | OSS (MIT→agpl/core) + Cloud | tracing LLM maduro, datasets/evals, OSS self-hosted | backend pesado (ClickHouse/Postgres/Redis), não local-first leve |
| **LangSmith** | SaaS proprietário | integração profunda LangChain, evals | lock-in LangChain, cloud-only |
| **Arize Phoenix** | OSS (ELv2) | OpenInference, spans em tempo real, evals | foco notebooks/ML; graph agêntico e replay limitados |
| **Helicone** | SaaS + self-host | proxy LLM simples, custo | modelo proxy não captura lógica de agente local |
| **LangWatch / Lunary / Traceloop (OpenLLMetry)** | misto | semconv OTel GenAI | cobertura parcial de custo/replay/evidência |

### 2. Observabilidade generalista com suporte GenAI

| Produto | Fortes | Lacunas |
|---|---|---|
| **Datadog LLM Obs** | APM maduro | caro, SaaS, não local-first, agentic graph fraco |
| **New Relic / Grafana (Tempo+OTel)** | OTel nativo | semântica de agente inexistente (span genérico) |
| **Sentry** | errors + tracing | foco exceções, não execuções de agente |

### 3. Ferramentas de debugging de agentes

| Produto | Notas |
|---|---|
| **AgentOps** | sessões/replay mas SaaS e SDK proprietário |
| **W&B Weave** | tracing + evals, ligado ao ecossistema W&B |
| **Honeycomb** | excelente query engine, sem conceitos agênticos nativos |

## O que o mercado confirma

1. **OTel + OpenInference venceram o transporte**: todo mundo convergiu para spans
   com atributos `openinference.*`/`gen_ai.*`. Forge Lens ingere exatamente isso.
2. **Token/cost accounting é commodity esperada** — mas quase ninguém separa
   custo *estimado* de *faturado* (distinção epistêmica que o Lens faz).
3. **Evals estão presentes mas desconectados do trace** — datasets e experimentos
   raramente referenciam spans/ferramentas específicas da execução.
4. **"Replay" quase sempre é marketing**: ou é re-render do trace (observed) ou
   re-execução opaca. Nenhum mainstream separa explicitamente os 3 modos.

## Lacuna que o Forge Lens ocupa

- **Local-first real**: SQLite WAL, zero dependência de nuvem/container/licença.
- **Graph agêntico temporal**: agente→ferramenta→modelo→delegação com evidência.
- **Debug dirigido por evidência**: hipóteses de causa-raiz com suporte,
  contradição, evidência faltante — não "AI magic" sem fundamento.
- **Epistemic spine**: `observed|imported|derived|inferred|unknown` em tudo;
  custo estimado ≠ custo faturado; token medido ≠ estimado.
- **Independência**: sem tie-in a framework de agente, provedor de LLM ou Forge.
