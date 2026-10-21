# ADR-0004 — Epistemic spine: provenance obrigatório

**Status**: ACCEPTED (ciclo 00)

## Contexto

Observabilidade agêntica mistura fatos (um span aconteceu), derivações
(custo estimado), e inferências (causa-raiz candidata). Sem distinção
explícita, o produto apresenta hipótese como verdade — o pior defeito
possível numa ferramenta de debugging.

## Decisão

`Provenance` (`observed|imported|derived|inferred|unknown`) é campo
obrigatório em Trace, Span, AgentExecution, ToolInvocation, Evidence,
EvaluationResult, TokenUsage, CostObservation. Regras: `inferred` nunca
vira `observed`; ausência fica `None`; custo `estimated` (derived) e
`billed` (observed) são campos separados.

## Consequências

- Toda feature futura (graph, root-cause, evals) herda a disciplina de
  rotular evidência — UI pode mostrar aresta `inferred` distinta de
  `observed`.
- Custo: campos extras e disciplina de preencher `measurement_source`/
  `assumptions` corretamente — aceito como custo de honestidade.

## Alternativas rejeitadas

- **Booleano "is_estimated"**: perde a diferença importado↔observado e
  não captura `unknown`.
- **Provenance só em Evidence**: demais entidades continuariam ambíguas.
