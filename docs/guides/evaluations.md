# Guia — Evaluations

> Estado: **`DOCUMENTED_ONLY`** (ciclo 07). O contrato `EvaluationResult`
> já existe e é versionado; runners, datasets e comparação são roadmap.

## Contrato já disponível

`EvaluationResult`: `evaluator(+versão)`, `dataset(+versão)`, `metric`,
`score`, `confidence`, `ground_truth_reference`, `trace_reference`,
`evaluated_at`, `provenance` (avaliações são `derived` por padrão).

## Princípios (roadmap)

1. **Datasets versionados** — um caso = input + expectativa + metadados;
   dataset_id+versão travam reprodutibilidade.
2. **Avaliadores declarativos** — exact-match, schema-check, custom;
   nenhum evaluator inventa ground truth.
3. **Link ao trace** — toda avaliação referencia `trace_reference`;
   score sem trace não é evidência de melhoria.
4. **Comparação honesta** — diff de experimentos reporta delta + intervalo
   de confiança quando computável, nunca "ganhou" por 0.1 com n=3.
5. **Regressão** — queda de métrica aponta spans/ferramentas candidatas por
   evidência, não por correlação vaga.
