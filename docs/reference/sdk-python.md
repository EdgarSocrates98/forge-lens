# Referência — SDK Python (`forge_lens.sdk`)

## `LensTracer`

```python
LensTracer(
    service_name="meu-agente",     # obrigatório → TraceSource.system
    exporter=HTTPExporter(...)|InMemoryExporter()|duck-type,
    on_flush=lambda trace: ...,    # callback por trace flushada
    on_export_error=lambda exc, trace: ...,  # falha de auto-flush
    auto_flush=True,               # flush quando o span raiz fecha
    session_id=None,
)
```

### Context managers

| Método | SpanKind | Convenções |
|---|---|---|
| `start_span(name, kind, attributes, trace_id, remote_parent)` | qualquer | base de todos |
| `agent(name, **attrs)` | `agent` | run de agente |
| `llm_call(model, provider=...)` | `llm` | `llm.model_name`, `llm.provider` |
| `tool_call(tool_name, arguments=?, capture_args=False)` | `tool` | `tool.parameters` só com `capture_args=True` |

`tracer.record_usage(span, input_tokens=…, output_tokens=…, …)` anexa
`forge_lens.token_usage` (`measurement_source=provider|estimated`).

Erros: exceção dentro do `with` → `status=error` + evento `exception`;
o erro **propaga** (não engolimos).

### Flush

`tracer.flush(trace_id=None)` — envia pendências ao exporter; retorna o
resultado do export ou `None` se vazio. `auto_flush` dispara ao fechar o
span raiz da trace.

Semântica de falha (observabilidade nunca quebra o observado):

- **Lote preservado**: se `export()` falha, a trace volta para
  `_pending` — o próximo `flush()` reenvia (nada é perdido).
- **Auto-flush nunca propaga**: exceção do exporter é engolida, contada em
  `tracer.export_errors` e reportada via `on_export_error` — jamais
  mascara a exceção da aplicação instrumentada.
- **`flush()` explícito propaga** o erro do exporter ao chamador
  (mas ainda preserva o lote para retry).

`trace_id` inválido passado a `start_span` não é corrigido
silenciosamente: o span ganha `forge_lens.trace_id_regenerated=true` +
`forge_lens.invalid_trace_id_supplied` e um id novo é gerado.

### Decoradores

```python
@span("nome", kind=SpanKind.INTERNAL, tracer=t)
def f(...): ...

@instrument_tool(name="search", capture_args=True, tracer=t)
def search(q): ...
```

## `SpanContext` / propagação

`SpanContext.create()`, `.from_headers(headers)` (W3C `traceparent`),
`.to_headers()`. Passe `remote_parent=ctx` a `start_span` para continuar
uma trace remota. `current_span()` lê o topo da pilha (contextvar).

## `HTTPExporter`

```python
HTTPExporter(endpoint="http://127.0.0.1:7777", api_key=None,
             project_id="default", timeout_s=10)
```

`export(traces, source)` → `POST /v1/traces` (stdlib urllib, zero deps de
rede adicionais). `ExportError` em falha HTTP/transporte.

## `InMemoryExporter`

Acumula `traces` — testes, captura offline, export para arquivo.
