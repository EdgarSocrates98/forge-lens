# Guia — Instrumentar um agente

## Padrão recomendado

Envolva **unidades de trabalho reais** — não linhas soltas:

```python
with tracer.agent("researcher", **{"agent.role": "web-research"}):
    plan = make_plan()
    with tracer.llm_call("claude-sonnet-4", provider="anthropic") as s:
        resp = call_model(plan)
        tracer.record_usage(s, input_tokens=resp.usage.input,
                            output_tokens=resp.usage.output)
    for item in resp.todos:
        with tracer.tool_call("web_search", capture_args=True,
                              arguments={"q": item.query}):
            results = search(item.query)
```

## Regras práticas

1. **Um `agent` por run** — o span raiz fecha a trace e dispara auto-flush.
2. **Tokens só quando reportados** — se o provider não devolve usage, não
   chute; `record_usage` aceita só o que existe.
3. **`capture_args=True` consciente** — argumentos podem conter dados
   sensíveis; no servidor, `capture=metadata` ainda os descarta.
4. **Erros propagam** — não engula exceções dentro de spans; `status=error`
   + evento `exception` já são registrados.
5. **Delegação entre processos**: propague `traceparent` (W3C) e continue
   com `remote_parent=SpanContext.from_headers(headers)` no receptor.

## Decoradores (azúcar)

```python
@span("plan")
def plan(...): ...

@instrument_tool(name="search", capture_args=True)
def search(q): ...
```

## TypeScript

Mesma forma com `tracer.agent/llmCall/toolCall` — ver
`docs/reference/sdk-typescript.md`.
