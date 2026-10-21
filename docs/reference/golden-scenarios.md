# Referência — Golden Scenarios (`tests/golden/`)

Corpus declarativo e determinístico de cenários ponta-a-ponta. Cada
arquivo em `tests/golden/scenarios/*.json` é uma especificação
auto-contida do comportamento **real** do produto — não aspiracional:
quando o código muda de semântica, o golden só muda depois que um humano
confirma a nova verdade.

Diferente de `tests/fixtures/` (3 fixtures ad-hoc usados pelo teste de
contrato de normalização), o corpus cobre ingest, dedupe, sanitize,
integridade, limites, tenancy, papéis, roll-ups, ops, avaliação,
datasets, retenção, auditoria, MCP, replay e grafo — 47 cenários em 16
categorias.

## Execução

```bash
uv run pytest tests/golden -q
```

Um único runner (`test_golden_scenarios.py`) carrega todos os arquivos
em ordem de path, valida o schema do corpus e executa cada cenário num
harness isolado: `SQLiteTraceStore` em `tmp_path`, `LensConfig` do
`setup.config`, `TestClient` real sobre `create_app(store=, config=)`.
Nada é mockado; o servidor MCP roda de verdade sobre stdio em memória.

Dois testes protegem o corpus além dos cenários:

- `test_corpus_is_present_and_unique` — exige `MIN_SCENARIOS = 30` e
  ids únicos (encolher o corpus é decisão de revisão, não acidente).
- `test_corpus_schema_valid` — qualquer JSON malformado falha a suíte
  nomeando o arquivo e a chave ofensora. Um cenário inválido nunca é
  silenciosamente ignorado.

## Formato do arquivo

```json
{
  "scenario_id": "dedupe-identical-reingest",   // == nome do arquivo (sem .json)
  "title": "frase curta do comportamento afirmado",
  "category": "dedupe",                          // ver tabela de cobertura
  "setup":    { ... },                           // opcional: ambiente
  "input":    { "op": "...", ... },              // obrigatório: a operação
  "expected": { ... },                           // obrigatório: afirmações
  "forbidden": { ... },                          // obrigatório: negações
  "evidence_required": true,                     // opcional
  "provenance": "derived",                       // opcional
  "notes": "por que este comportamento é o esperado"
}
```

Campos obrigatórios: `scenario_id`, `title`, `category`, `input`,
`expected`, `forbidden`, `notes`. Chaves desconhecidas em qualquer
nível são rejeitadas (o corpus é estrito mesmo onde os contratos do
produto são permissivos).

### `setup`

| chave           | efeito                                                        |
|-----------------|---------------------------------------------------------------|
| `config`        | kwargs de `LensConfig`: `capture`, `auth_mode`, `max_payload_bytes`, `rate_limit_per_minute`, … |
| `api_keys`      | chaves gerenciadas: `key_id`, `name`, `secret`, `role`, `projects` |
| `store_traces`  | `Trace`s inseridas direto no store — sem pipeline (preserva `output.value`, flags declaradas) |
| `ingest_traces` | `Trace`s que passam pelo pipeline de verdade (sanitize + flags) |
| `requests`      | requests HTTP de setup; devem responder < 300                   |

Use `store_traces` quando o cenário é sobre um consumidor (eval,
loops, replay, grafo) e precisa de atributos que `capture=metadata`
descartaria na ingest. Use `ingest_traces` quando a semente em si deve
ser evidência de ingest (flags, dedupe, órfãos).

### `input.op`

| op        | efeito                                                                   |
|-----------|--------------------------------------------------------------------------|
| `http`    | `requests`: lista de `{method, path, headers?, params?, json?, content?}` executada no TestClient |
| `pipeline`| `traces` (lista de objetos `Trace`), opcional `times`/`payload_bytes` — roda `IngestPipeline.ingest` |
| `engine`  | `function` + `trace_id` (+`project_id`/`params`) — função do motor sobre o trace armazenado: `detect_loops`, `classify_failure`, `replay_timeline`, `build_trace_graph`, `inspect_trace`, `trace_efficiency` |
| `mcp`     | `frames`: linhas JSON-RPC enviadas ao servidor stdio; saída indexada por `id` + `_rc` |

### `expected` / `forbidden`

- HTTP: `status` (todos), `statuses` (por posição), `responses[i]`
  (`status`/`json`/`json_contains`/`body_contains`), `json` +
  `json_contains` + `body_contains` contra a **última** resposta.
- Pipeline: `result`/`results` (subconjunto de `IngestResult`),
  `error` (`type`/`contains`).
- Engine/MCP: `output` (subconjunto), `output_contains`.
- `json`/`output` casam por **subconjunto profundo**: todo campo
  declarado deve existir e casar; listas exigem o mesmo comprimento.
- `json_contains`/`output_contains`: `{path.dotted: [substrs]}` —
  cada agulha deve aparecer no JSON resolvido no path.
- `store`: `trace_count`, `span_count`, `traces.{tid}` (subconjunto do
  dump), `spans.{tid}/{sid}`, `span_flags`, `span_flag_absent`,
  `span_attrs`, `sessions`, `tools`, `audit` (cada linha declarada
  deve casar com alguma linha real por subconjunto).
- `forbidden`: `status_not`, `body_not_contains`,
  `output_not_contains`, `store_json_not_contains`,
  `audit_not_contains`, `trace_ids_absent`.
- `evidence_required: true` — todo dict no output que carrega
  `span_ids`/`evidence`/`details`/`observations` deve tê-lo não-vazio.
- `provenance: "<valor>"` — todo campo `provenance` presente no output
  deve ser exatamente esse (ex.: `derived` em resultados de
  avaliação). Falha se nenhum campo de provenance existir.

## Cobertura (47 cenários)

| categoria    | n | cobre                                                                 |
|--------------|---|------------------------------------------------------------------------|
| `ingest`     | 3 | nativo feliz, OTLP feliz, preenchimento de projeto pelo envelope      |
| `dedupe`     | 3 | re-entrega idêntica, merge de conclusão tardia, conflito de identidade |
| `sanitize`   | 5 | chave sensível, valor-credencial, drop em metadata, retenção em full, e-mail |
| `integrity`  | 9 | quarentena OTLP, missing_parent, orphan, span_id duplicado, clock_skew, partial_trace, unknown_tokens, sent_at do envelope, eventos truncados |
| `limits`     | 3 | 413 por bytes, 413 por spans, 429 por rate limit                       |
| `tenancy`    | 3 | leitura cross-project, escrita cross-project, leitura sem project      |
| `roles`      | 2 | reader não escreve, writer não administra                              |
| `rollups`    | 2 | sessões, ferramentas                                                   |
| `ops`        | 4 | loops (idêntico, retry+no-progress), classificação (rate_limit, ciclo) |
| `eval`       | 4 | bateria das 12 chaves, nulls honestos, gate pass, gate fail            |
| `datasets`   | 2 | append-only 409, versão órfã 404                                       |
| `retention`  | 1 | prune antes do cutoff                                                  |
| `audit`      | 2 | key.create, auth negada                                                |
| `mcp`        | 2 | erro de argumento sobrevive, método desconhecido                       |
| `replay`     | 1 | ordenação determinística da timeline                                   |
| `graph`      | 1 | ciclo reportado em stats                                               |

## Comportamentos reais documentados (não corrigidos)

Estes cenários capturam o que o código **faz hoje** — incluindo
decisões que podem surpreender:

- `dedupe-late-completion-merge` — a conclusão tardia limpa
  `missing_end` mas **`partial_trace` permanece** no span: o refresh de
  flags só retira `missing_parent`/`orphan`/`missing_end`; a evidência
  de entrega incompleta sobrevive à conclusão.
- `integrity-duplicate-span-id` — dois spans com o mesmo `span_id` no
  mesmo trace **não são rejeitados nem duplicados**: o segundo passa
  pelo caminho de merge (`updated_spans=1`, atributos unidos, nome do
  primeiro vence) e, como `name` difere, ganha `conflict`.
- `eval-compare-gate-pass` — `insufficient_data` com n<10 por lado: o
  teste estatístico recusa fingir significância mesmo quando o gate
  passa nas taxas observadas.
- `integrity-clock-skew` — `clock_skew` é flag sem warning; o skew do
  envelope (`sent_at` anterior ao início) é warning sem flag. Assimetria
  deliberada registrada como golden.

## Áreas honestamente não cobertas

- **Significância estatística completa** — o corpus valida
  `insufficient_data`/`not_tested` mas não gera coortes grandes o
  bastante para `significant` (custo/benefício de 20+ traces por lado).
- **SSE stream** (`/v1/traces/{id}/stream`) — o DSL declara corpos
  JSON; SSE ficou de fora deliberadamente.
- **Alertas salvos** (`/v1/alerts/rules` + `use_saved`) — dependem de
  arquivo em `data_dir`; cobertos apenas via avaliação inline implícita.
- **Experimentos** (`/v1/experiments/*`) — a superfície é recente
  (Cycle 13); ainda sem cenário — o append-only de runs é análogo ao de
  datasets.
- **Export/import e CLI** — o corpus mira HTTP/pipeline/engine/MCP;
  comandos CLI são cobertos por seus próprios testes.
- **`trace_cost`/`recommendations`/especialistas** — superfícies
  inteligentes com output rico; nenhum golden ainda.

## Adicionando um cenário

1. Crie `tests/golden/scenarios/<categoria>-<slug>.json` —
   `scenario_id` deve ser idêntico ao nome do arquivo.
2. Rode o comportamento de verdade primeiro (REPL ou teste) e escreva
   `expected` a partir do observado — nunca do desejado.
3. Inclua `forbidden` sempre que o cenário implicar uma negação real
   (segredo que não vaza, tenant que não responde, contagem que não
   infla).
4. `notes` explica **por que** — a referência ao código ou à decisão
   vale mais que a asserção sozinha.
5. `uv run pytest tests/golden -q` — um cenário que falha no primeiro
   run é suspeito: confirme se a expectativa ou o produto está errado.
