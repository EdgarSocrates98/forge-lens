# Referência — Experiments (v1)

Um `Experiment` é um *spec A/B persistido*: dois cohorts declarados
(`trace_ids` + labels + `config` livre) e a lista de `evaluators` — a
mesma forma aceita por `/v1/eval/compare`. Ele fecha a lacuna do
audit 06.3: sem persistência, cada compare era efêmero e
irreprodutível; agora o spec fica gravado e cada execução vira uma
linha append-only em `experiment_results`.

Contrato: `schemas/v1/experiment.json`
(`forge_lens.contracts.v1.Experiment`, `schema_version = "1.0.0"`).

## Modelo

| campo | tipo | notas |
|---|---|---|
| `experiment_id` | str (1–256) | vem do path no PUT |
| `project_id` | str | default `"default"` — escopo de tenant como datasets |
| `name`, `description` | str? | metadados declarados |
| `created_at` | ISO-8601? | metadado de escrita — cliente fornece; `null` = não observado (a API nunca lê relógio para isso) |
| `dataset_id`, `dataset_version` | str? | pin da verdade avaliada; `dataset_version` exige `dataset_id`. Vira o label `id@version` no report |
| `cohorts` | `{a, b}` | cada lado: `trace_ids` (1–500, obrigatório), `label?`, `config?` — `config` é dict livre para versões declaradas de agent/model/prompt/tools: **labels, nunca resolvidos** |
| `evaluators` | list (1–100) | specs `{name, params}` idênticas às de `eval/run`/`eval/compare` |
| `status` | `open\|completed` | lifecycle honesto — `open` até a primeira run; `completed` depois. Re-PUT não reseta (runs antigas ficam intactas) |
| `result_ref` | str? | `run:<id>` da última run gravada — campo derivado, nunca declarado |
| `provenance` | `imported` | o spec é declarado por um chamador externo — nunca `observed` |
| `metadata` | dict | livre |

JSON não-finito (`NaN`, `Infinity`, `-Infinity`) no corpo do PUT →
`422`. Spec declarado com número que não existe em JSON seria
silenciosamente gravado como `null` — a borda rejeita em vez de coagir.

## Rotas

Auth: PUT e `run` exigem role **writer** (`check_auth` — em `keys`
mode, `401` sem chave, `403` para reader). GETs seguem `read_deps`.
Escopo por projeto idêntico ao de datasets: `project_id` omitido
resolve `cfg.project_id`; chave com escopo recebe `403` fora dele.

### `PUT /v1/experiments/{experiment_id}`

Cria ou atualiza o **spec** (upsert — mesma convenção de `PUT
/v1/datasets/{id}`: `created_at`, `status` e `result_ref` não são
tocados; `status` pode ser passado explicitamente para reabrir).
Body validado: `cohorts` com `a` **e** `b` objetos, `trace_ids`
1–500 por lado, `evaluators` 1–100, `name` não-vazio quando presente.

Resposta: `{experiment_id, project_id, stored: true}`.
Erros: `400` JSON inválido, `401`/`403` auth/escopo, `413` payload,
`422` validação (inclui não-finito, `dataset_version` sem
`dataset_id`, `status` fora de `open|completed`).

### `GET /v1/experiments?project_id&limit≤500&offset`

Envelope `{items, limit, offset}` — itens resumidos
(`experiment_id, project_id, name, status, result_ref, created_at`),
ordenação determinística por `experiment_id`.

### `GET /v1/experiments/{experiment_id}?project_id&limit≤500&offset`

O spec completo + `{runs, run_limit, run_offset}` — `limit`/`offset`
paginam **as runs embutidas** (append-only, `ORDER BY run_id`).
`404` quando o experimento não existe no projeto resolvido.

### `POST /v1/experiments/{experiment_id}/run`

Executa o spec gravado pelo **mesmo caminho** de `/v1/eval/compare`:
resolve os `trace_ids` dos cohorts no projeto do experimento →
`evaluate_traces` por lado → `compare_runs` → `gate` opcional
(`apply_gate`, mesmo contrato). Depois **apenda** o report em
`experiment_results` e marca `status=completed` +
`result_ref=run:<id>` na mesma transação.

Body (JSON object obrigatório; todos os campos opcionais):
`{"project_id": "...", "gate": {...}}`.

Resposta: `{experiment_id, project_id, run_id, run_at, report}` —
`report` é o payload integral de `compare_runs` (+ `gate` quando
pedido). `run_at` é timestamp de borda (`wall_clock_ns` na API —
único lugar onde relógio real é lido; o report em si é
determinístico).

Erros: `404` experimento ausente ou `trace_id` de cohort sem trace
persistida (evidência evaporada → falha alta, nunca comparação
parcial vendida como completa); `422` experimento sem `evaluators`,
`gate` malformado; `401`/`403` auth/escopo.

## Garantias e limites honestos

- **Append-only**: cada run é uma linha nova em
  `experiment_results`; nada reescreve runs anteriores. Re-editar o
  spec após runs não apaga histórico — cada report embute os inputs
  resolvidos (`trace_ids`, dataset label, deltas).
- **Cohort `config` é label, não fato**: versões de
  agent/model/prompt são strings declaradas — a plataforma as
  preserva verbatim e nunca as promove a evidência observada.
- **Cohorts não cruzam tenants**: ao contrário do compare ad-hoc
  (que aceita `project_id` por lado), a run do experimento resolve
  traces só no projeto do experimento.
- **Significância estatística** segue as mesmas regras de
  `eval/compare` (z-test/Mann-Whitney só com amostra mínima —
  ver `docs/reference/api.md`).
- Runs além de 500 pedem `offset` na leitura — o limite é o mesmo
  de todas as coleções.
