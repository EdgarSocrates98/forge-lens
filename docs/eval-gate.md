# Gate de avaliação em CI — `scripts/eval_gate.py`

> Estado: **implementado** (ciclo 13.1). Gate determinístico de regressão
> sobre traces armazenadas — roda **offline**, só com stdlib + `forge_lens`
> instalado, sem nenhuma chamada de rede.

## O que é

`scripts/eval_gate.py` é o ponto de entrada de CI do subsistema de
avaliação. Ele pega duas coortes de traces já armazenadas no SQLite do
forge-lens — **baseline** (referência, ex.: última release aprovada) e
**candidate** (a mudança sob revisão) — e as empurra pelo **mesmo**
caminho `evaluate_traces` → `compare_runs` → `apply_gate` usado pelos
endpoints `POST /v1/eval/run` e `POST /v1/eval/compare`. O resultado do
gate vira um código de saída amigável para CI, um relatório JSON
determinístico no stdout (consumível por máquina) e um resumo humano no
stderr (log do job).

A promessa central: **evidência fina nunca reprova um build — e nunca o
aprova em silêncio.** Regressão exige evidência observada e testada;
veredito impossível é reportado como `insufficient_data`, não como falha
e não como passe vacuoso.

## Interface

```bash
python scripts/eval_gate.py \
    --db .forge-lens/lens.db \
    --dataset specs/eval-suite.json \
    --baseline cohorts/base.json \
    --candidate '{"trace_ids": ["abcd…"]}' \
    --gate '{"metrics": {"error_free/no_error_spans": {"max_drop": 0.05}},
             "fail_on_significant_regression": true}'
```

| Flag | Formato | Obrigatório |
|---|---|---|
| `--dataset` | `ID`, `ID@version`, caminho de arquivo JSON ou `@arquivo` | sim |
| `--baseline` | spec de coorte — JSON inline, `@arquivo` ou lista crua de ids | sim |
| `--candidate` | idem `--baseline` | sim |
| `--db` | caminho do SQLite do forge-lens (default: config resolvida) | não |
| `--gate` | spec do `apply_gate` — JSON inline ou `@arquivo` | não (default abaixo) |
| `--project` | projeto default para coortes e lookup do dataset | não |

Valores `--dataset`/`--baseline`/`--candidate`/`--gate` aceitam três
formas: `@arquivo` (sempre lê do disco), JSON inline (`{...}`/`[...]`) ou
um caminho de arquivo existente. Um valor que não é nenhum dos três é
erro de uso — nunca uma adivinhação.

### `--dataset` — a bateria de avaliadores

Resolve de duas formas:

1. **Arquivo JSON** — `{"evaluators": [{"name": ..., "params": {...}},
   ...], "dataset_id"?, "dataset_version"?}` ou uma lista crua
   `[{"name": ...}, ...]`.
2. **Dataset armazenado** — `--dataset qa-suite` ou `qa-suite@v3`. Os
   specs são lidos de `metadata.evaluators` do registro (ou da versão,
   quando `@version` é passado). **Convenção documentada desta
   ferramenta**: a versão do dataset fixa ao mesmo tempo a verdade e a
   bateria que a verifica — o engine nunca inventa mapeamento
   caso→spec. Sem `metadata.evaluators` → erro de uso explicando como
   declarar a bateria.

O limite é o mesmo da API: 1..100 specs.

### `--baseline` / `--candidate` — as coortes

```json
{
  "trace_ids": ["<32-hex>", "…"],
  "label": "release-1.4",
  "project_id": "checkout",
  "dataset": "qa-suite@v3"
}
```

ou uma lista crua `["<trace-id>", ...]`. Limites: 1..500 ids não-vazios
(mesmo bound do endpoint). `label`, `project_id` e `dataset` são
opcionais; `project_id` cai no `--project`/config quando ausente.

**Todo id precisa resolver para uma trace armazenada.** Ids ausentes são
reportados em `missing_trace_ids` e o veredito é `insufficient_data` —
uma coorte que referencia evidência evaporada não vira comparação
parcial.

### `--gate` — os critérios

Spec exatamente o do `forge_lens.eval.compare.apply_gate`:

```json
{
  "metrics": {
    "error_free/no_error_spans": {
      "min_pass_rate_b": 1.0,
      "max_drop": 0.05
    },
    "token_f1/score": { "max_drop": 0.10 }
  },
  "fail_on_significant_regression": true
}
```

- `metrics.<avaliador>/<métrica>.min_pass_rate_b` — o pass_rate do
  candidato precisa atingir o valor.
- `metrics.<avaliador>/<métrica>.max_drop` — `pass_rate_a − pass_rate_b`
  não pode exceder o valor. Se qualquer lado é nulo (métrica avaliada
  mas não observada), o critério falha por evidência ausente →
  `insufficient_data`, nunca passe vazio.
- `fail_on_significant_regression` — reprova quando qualquer delta tem
  `significance == "significant"` **e** `delta < 0` (cobre métricas
  contínuas via MWU + delta de média, além das binárias).

Um spec de gate que não declara critério nenhum (`{}`, `metrics` vazio
sem `fail_on_significant_regression: true`) é **erro de uso** — um gate
que não afirma nada não pode "passar".

Default quando `--gate` é omitido: `{"fail_on_significant_regression":
true}`.

## Como o veredito é decidido

Os `failures` do `apply_gate` são classificados:

- **Falha com evidência** → `regression`: `pass_rate_b below minimum`
  com `observed` numérico, `pass_rate dropped more than allowed`,
  `statistically significant regression`.
- **Falha por evidência ausente** → `insufficient_data`: `metric not
  evaluated — cannot gate on unmeasured evidence` (a chave do gate não
  foi avaliada), `pass_rate_b below minimum` com `observed: null`,
  `max_drop unmeasured — pass rate missing on one or both sides`
  (`max_drop` sobre pass_rate nulo **nunca** passa em vacuidade).
- **Spec malformado** (`gate.metrics must be an object`, `metric gate
  spec must be an object`) → erro de uso.

Sem falhas, o passe ainda precisa ser honesto: critérios de `metrics`
que passaram necessariamente observaram valores reais (pass_rate nulo
teria falhado); um gate só-`fail_on_significant_regression` exige que
ao menos um teste tenha rodado (`p_value` presente ou
`significance == "not_computable"` — variância zero com amostras
idênticas é **igualdade provada**, evidência real de ausência de
regressão). Caso contrário → `insufficient_data`.

Métricas com `significance` em `insufficient_data`/`not_tested` viram
warnings sempre que ocorrem — um gate verde sobre dados finos é
visivelmente fino no relatório.

## Códigos de saída

| Código | Significado |
|---|---|
| `0` | gate passou — todo critério declarado foi avaliado sobre evidência observada e nenhum falhou |
| `1` | regressão — ao menos uma falha de gate com evidência observada por trás |
| `2` | dados insuficientes — traces não resolvidas, métricas não medidas, ou nenhum teste atingiu o piso estatístico |
| `64` | erro de uso/configuração — entrada ilegível, spec malformado, gate que não afirma nada (sysexits `EX_USAGE`) |
| `70` | erro interno — falha inesperada do engine (sysexits `EX_SOFTWARE`); o relatório parcial ainda vai ao stdout |

> Por que 64/70? O convencional exit 2 do argparse já significa
> *dados insuficientes* aqui — um veredito sobre dados, não um erro de
> linha de comando. Erros de uso e falhas internas usam os códigos BSD
> `sysexits` para não colidirem com 0/1/2.

## Estatística por trás

O gate herda toda a semântica do compare (detalhes em
`docs/guides/evaluations.md`):

- **Métricas binárias** — z-teste de duas proporções sobre o pass_rate;
  intervalo de Wilson a 95% por lado em `ci_a`/`ci_b` (mantém cobertura
  honesta em n pequeno e nas bordas 0/1; reportado mesmo abaixo do piso,
  como descritivo).
- **Métricas contínuas** — Mann-Whitney U sobre os scores por
  resultado, aproximação normal com correção de continuidade e de
  empates.
- **Piso de amostra** — n ≥ 10 por lado; abaixo disso o teste é
  recusado (`insufficient_data`), nunca aproximado em silêncio.
- **Múltiplas comparações** — mais de uma métrica testada emite
  `multiple_comparisons` ("k tests at alpha=0.05 unadjusted — interpret
  jointly with care"); não há correção de multiplicidade.
- **`score=None` nunca é zero** — evidência ausente é contada em
  `nulls`, não fabricada.

## Saída

- **stdout** — o relatório JSON completo (`schema:
  "forge-lens/eval-gate@1"`): dataset resolvido, specs da bateria,
  coortes (ids pedidos/resolvidos/faltantes), o `compare` completo
  (deltas, CIs, p-values), o resultado do `apply_gate`, warnings e o
  `verdict` (`status`, `exit_code`, `reason`, `evidence_failures`,
  `missing_evidence_failures`).
- **stderr** — resumo humano determinístico para o log do CI:
  status, dataset, contagens por coorte, uma linha por delta
  (`pass_rate a→b`, `delta`, `sig`, `p`), falhas `FAIL`/`THIN` e
  warnings.

O relatório é **determinístico**: nenhum wall-clock no caminho de eval,
coleções emitidas ordenadas — o mesmo input produz o mesmo JSON byte a
byte.

## Integração em GitHub Actions

`docs/eval-gate-workflow-example.yml` traz um workflow comentado e
copiável para **outros** projetos consumirem — instala `forge-lens`,
roteia runs instrumentados para o store e executa o gate contra a
coorte baseline. O exemplo fica em `docs/` justamente para **não**
ativar nada ao ser copiado por engano para `.github/workflows/`.
