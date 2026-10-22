# Integração E2E — the-forge → Forge Lens

Integração **real, offline e ponta a ponta** entre um sibling Forge e o Forge
Lens. Tudo aqui foi executado de verdade — os artefatos em `evidence/` são
saídas sanitizadas das chamadas reais, não montagem manual.

## O que foi integrado

**the-forge** (`../the-forge`, v0.5.6) — o control plane do ecossistema,
escolhido porque é o único sibling que **emite spans nativos por execução**:
cada `theforge ask` grava `.forge/runs/<run_id>/telemetry.json` no contrato
`theforge/RunTelemetry/v1`, com `start_ms`/`duration_ms` medidos pelo recorder
monotônico do próprio theforge, `status` por span (`ok`/`error`) e atributos
de provider (`action`, `capability`, `round`).

## Mecanismo — exatamente o que cada camada é

| Camada | Provenance | Origem real |
|---|---|---|
| Root span (`theforge ask …`) | `observed` | Instrumentação explícita do harness: `LensTracer.start_span` envolve o `subprocess.run` do CLI; wall-clock e `process.exit_code` medidos pelo harness. |
| Spans de fase (`theforge.scan`, `theforge.routing`, `theforge.planning`, `theforge.context`, `theforge.verification`, `theforge.synthesis`) | `imported` | `telemetry.json` do run — medidos pelo theforge (relógio monotônico). Não há re-medição. |
| `provider:<id>` | `imported`, kind=`tool` | O span nativo do despacho ao provider (subprocess Forge Protocol). `tool.name=theforge.provider:<id>`. |
| Status/erro do run | `imported` | `receipt.json` (`status`, `error.code`, `error.detail`). |
| Métricas agregadas (`theforge.metric.*`) | `imported` | Só campos `Metric.kind == "measured"` do telemetry; `unknown` nunca vira `0` — sai listado em `theforge.unknowns`. |

### Âncora de tempo (derivação declarada, não medida)

Os spans do theforge são offsets monotônicos (`start_ms` desde a criação do
recorder), não timestamps de parede. Para posicionar no eixo de tempo o
importer usa, nesta ordem: `receipt.started_at` → `task.created_at` →
`telemetry.created_at` menos o fim do último span. O campo usado fica
registrado no resumo (`anchored_at`). **Durações e offsets são os valores
medidos pelo theforge, sem alteração**; só a origem do eixo é derivada.

### Transporte e leitura

- Escrita: `HTTPExporter` → `POST /v1/traces` contra uvicorn real (socket
  livre) — mesmo caminho de produção do SDK.
- Leitura: REST (`LensClient`/`GET /v1/traces`, `…/failure`) **e** MCP stdio
  (`python -m forge_lens mcp-serve --db …` — `initialize`, `tools/list`,
  `tools/call` reais por JSON-RPC linha a linha).

## Cenários exercitados (runs reais)

1. **OK** — `theforge ask "echo hello …" --use echo-forge`: o provider
   builtin `echo-forge` (`trust=builtin`) responde de verdade; run grava 8
   spans nativos, `receipt.status=ok`.
2. **FALHA controlada** — `theforge ask "explode the fixture"
   --use failing-forge --capability demo.failcrash --allow-unverified`: o
   provider fixture `examples/forge-integration/fixtures/failing_provider.py`
   (declarado no `providers.toml` do workspace) responde `describe`/`health`
   honestamente e **quebra com exit 3 no `execute`**. O theforge grava
   `provider:failing-forge` com `status="error"` e
   `receipt.error.code=FORGE-PROTO-EXIT` → o Lens classifica a trace como
   **`tool_error`/`observed`** (evidência: o span de tool que errou).

Isolamento: o harness exporta `THEFORGE_CONFIG_DIR`/`THEFORGE_CACHE_DIR` para
dentro do workspace temporário — nenhum provider/cache do usuário é lido.

## Reproduzir

```bash
# demo completo (sobe API, roda os 2 runs reais, exporta, lê via REST+MCP,
# grava evidência sanitizada)
.venv/bin/python examples/forge-integration/run_demo.py

# opções
.venv/bin/python examples/forge-integration/run_demo.py \
    --data-dir /tmp/forge-demo --evidence-dir docs/integration/evidence

# só o teste de integração (mesmo fluxo, asserts sobre dados reais)
.venv/bin/python -m pytest tests/integration/test_forge_e2e.py -xvs
```

Resolução do binário the-forge (`forge_bridge.find_theforge`):
`FORGE_LENS_THEFORGE` → `../the-forge/.venv/bin/theforge` →
`../the-forge/.venv/bin/python -m theforge` → `theforge` no PATH.
**Sem o sibling**, demo e teste caem para `fixtures/captured/{ok,fail}/` —
artefatos reais gravados de runs reais anteriores (sanitizados), importados
pela mesma função; tudo sai `provenance=imported` + atributo
`forge_lens.captured_fixture=true`. Nunca sintético.

## Inventário de hooks de observabilidade nos siblings

Levantamento real do checkout (`../`); a coluna "classe" segue
(a) hook direto, (b) instrumentação explícita possível, (c) formato
importável, (d) só correlação, (e) indisponível.

| Sibling | Superfície encontrada | Classe |
|---|---|---|
| **the-forge** | `theforge/RunTelemetry/v1` spans por run em `.forge/runs/<id>/telemetry.json`; `theforge/ExecutionObservation/v1` em `.forge/metrics/observations.jsonl`; `correlation_id`/`parent_run` no telemetry; `RunTelemetry.spans` com status/attrs | **(a) + (c)** — integrado aqui |
| **spark-forge-aws** | `sparkforge-aws telemetry export --run-id …` grava `.sparkforge_aws/telemetry/<run>.traces.jsonl` + `.metrics.jsonl` em **OTLP/JSON** (semconv GenAI, commit fixado); fonte = ledger de tool spans medidos por `call_tool` | **(a) + (c)** — emissor OTLP real; exige um run gravado em `traces.db` (não exercitado neste ciclo) |
| **forge-doctor-api** | `scan --format json|jsonl` e `lab --json` produzem relatórios determinísticos com timings (`elapsed_ms`, `peak_bytes`) — importáveis; `_portal_telemetry.py` = buffer em processo só quando o portal adapter sobe (nada persiste); `analyzers/runtime/otlp.py` **consome** OTLP como evidência de entrada (não emite) | **(c)** parcial; nenhum span emitido |
| **api-forge** | `contracts/agent_telemetry.py`: contratos locais fechados OTel-shaped para spans de agente/tool; `field/record.py` projeta artefatos de run em `FieldRun` | (b)/(c) — contrato existe; emissão não exercitada |
| **platform-forge** | `observe/otel.py` correla spans a partir de JSONL — **consumidor**, não emissor | (d) |
| **forge-doctor-data**, **spark-forge-azure**, **forge-portal** | `_portal_telemetry.py`/`portal_core/telemetry.py`: `forge/PortalEvent/1` com `trace_id`/`span_id`/`duration_ms` — buffer **em processo**, sem sink nem persistência | (d) — correlação apenas, nada a importar offline |
| **forge-cli** | CLI TS de orquestração/instalação; nenhum artefato de telemetria emitido | (e) indisponível |
| **data-sentinel-forge** | `connectors/telemetry/` — conector de ingestão de telemetria (consumidor) | (d) |

## Evidência gerada

`docs/integration/evidence/` (tudo sanitizado: caminhos absolutos viram
`<data-dir>`/`<repo>`/`<forges>`/`<home>`; ids, timestamps e contagens são os
reais gravados):

| Arquivo | Conteúdo |
|---|---|
| `00-demo-metadata.json` | mecanismo, argv resolvido, ids de run/trace |
| `01-run-summary.json` | resumo por run: status, spans importados, âncora, métricas medidas, unknowns, routing |
| `02-api-list-traces.json` | `GET /v1/traces` real |
| `03-api-traces.json` | `GET /v1/traces/{id}` das duas traces (spans + provenance) |
| `04-api-failure-classification.json` | `GET /v1/traces/{id}/failure` → `tool_error/observed` |
| `05-mcp-exchange.json` | transcript MCP real: initialize, tools/list, `lens.list_traces`, `lens.failure`, `lens.stats` |

## Limitações — o que NÃO é capturado

- **Tokens/custo por chamada**: o core do theforge é determinístico e não
  chama LLM; `tokens` permanece `unknown` (honesto). O `provider_ms` cobre só
  o subprocess Forge Protocol — tokens internos de um specialist ficariam no
  specialist (`NativeTrace.ref` existe no contrato, mas `echo-forge` reporta
  `null`; nada foi correlacionado além do run).
- **Timestamps de parede por span**: só offsets monotônicos existem; a posição
  no tempo é derivada da âncora documentada (precisão ~ms, skew do `fork` do
  subprocess incluído na margem).
- **stderr/stdout completos do provider**: o run guarda só o erro de
  protocolo resumido em `receipt.error.detail`; a saída completa do provider
  fica em `.forge/runs/<id>/work/` fora do escopo importado.
- **Spans internos do specialist**: federação de trace mantém spans internos
  dentro do specialist; o Lens vê o boundary span `provider:<id>`, não o
  interior.
- **Pipelining/streaming**: `telemetry.json` é lido ao fim do run — não há
  observação em tempo real do progresso (o artifact só existe completo).
- **Correlação multi-run**: `correlation_id`/`parent_run` existem no contrato
  mas os runs do demo são independentes (nenhum encadeamento real exercitado).
