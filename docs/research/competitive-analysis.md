# Competitive Analysis — Forge Lens vs. referências

> Ciclo 00. Comparação focada nos critérios que o roadmap do Lens exige.

## Matriz de capacidades

| Capacidade | Langfuse | Phoenix | LangSmith | Datadog LLM | **Forge Lens (meta)** |
|---|---|---|---|---|---|
| OTLP/OTel ingest | parcial | ✅ (OTLP) | SDK próprio | ✅ APM | ✅ OTLP/HTTP JSON |
| OpenInference attrs | parcial | ✅ nativo | ❌ | parcial | ✅ mapa dedicado |
| Local-first leve (SQLite) | ❌ ClickHouse+PG | parcial | ❌ | ❌ | ✅ SQLite WAL |
| Agent/tool graph | sessões | trace tree | runs/tree | spans | ✅ graph temporal |
| Replay observado | re-render | ✅ | parcial | ❌ | ✅ (padrão) |
| Replay simulado/seguro | ❌ | ❌ | ❌ | ❌ | roadmap ciclo 05 spec |
| Root-cause c/ evidência | scores | evals | evals | ❌ | ✅ hipóteses+evidência |
| Cost estimado vs faturado | estimado | estimado | estimado | estimado | ✅ separados |
| Loop/no-progress detection | ❌ | ❌ | ❌ | ❌ | roadmap ciclo 07 |
| Evals linkadas a spans | parcial | ✅ | ✅ | ❌ | ✅ backend ciclo 06 |
| SDK zero-dep | ❌ | ❌ | ❌ | ❌ | ✅ TS (stdlib Py) |
| MCP instrumentation | ❌ | ❌ | ❌ | ❌ | opcional (ciclo 10) |
| Open-source real | AGPL/core+cloud | ELv2 | ❌ | ❌ | MIT |

## Posicionamento

- **Não competir** com APM generalista (Datadog/Grafana) — eles servem infra;
  o Lens serve *execuções de agente*.
- **Diferenciar** de Langfuse/Phoenix por local-first radical + epistemic spine
  + replay honesto + graph temporal. Ambos são inspiração de UX de trace.
- **Complementar**, não substituir: ingest OTLP permite receber spans de
  OpenLLMetry/OpenInference — o Lens vira o *destino local* de pipelines que
  outros já emitem.

## Riscos competitivos

- Langfuse/Phoenix podem adicionar "local mode" — mitigação: focar no que eles
  estruturalmente não farão (SQLite único, sem serviço de ingestão separado,
  prova de evidência, replay seguro).
- Semconv GenAI ainda é instável — mitigação: mapa de atributos tolerante
  (`openinference.*` + `gen_ai.*` + `llm.*`), flags de integridade honestas.
