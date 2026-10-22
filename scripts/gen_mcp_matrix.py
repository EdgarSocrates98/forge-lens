#!/usr/bin/env python3
"""Generate ``docs/reference/mcp-tools.md`` — the MCP tool matrix.

Single source of truth: the live registry in
``src/forge_lens/mcp_server.py`` (``_tools()``). The script imports the
real module — never a copy — so the doc can only drift when the code
drifts, and ``tests/unit/test_mcp_matrix.py`` fails on that drift.

Stdlib only. Deterministic output: tools sorted by name, params sorted
by schema order. Usage:

    .venv/bin/python scripts/gen_mcp_matrix.py            # reescreve o doc
    .venv/bin/python scripts/gen_mcp_matrix.py --stdout   # imprime, não grava
    .venv/bin/python scripts/gen_mcp_matrix.py --check    # exit 1 se dessincronizado
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from forge_lens.mcp_server import _tools  # noqa: E402

OUT_PATH = _REPO / "docs" / "reference" / "mcp-tools.md"

# Bound semantics authored in _call/_num/_page_args (mcp_server.py) —
# documented contract, behaviorally pinned by test_mcp_lifecycle.py:
# limit clamps to [0, 500], offset must fit sqlite int64, trace_id must
# be a non-empty string.
_PARAM_BOUNDS = {
    "trace_id": "string não-vazia",
    "limit": "int em [0, 500]",
    "offset": "int em [0, 2^63-1]",
}


def _params_cell(schema: dict[str, Any]) -> str:
    """``name*`` (required), ``name?`` (optional) with type + default."""
    props = schema.get("properties", {})
    if not props:
        return "—"
    required = set(schema.get("required", []))
    parts = []
    for pname, spec in sorted(props.items()):
        mark = "*" if pname in required else "?"
        default = f" ={spec['default']}" if "default" in spec else ""
        parts.append(f"`{pname}{mark}{default}` ({spec.get('type', '?')})")
    return "<br>".join(parts)


def _bounds_cell(schema: dict[str, Any]) -> str:
    notes = [
        f"`{p}` {note}"
        for p, note in sorted(_PARAM_BOUNDS.items())
        if p in schema.get("properties", {})
    ]
    return "; ".join(notes) if notes else "—"


def _errors_cell(name: str, schema: dict[str, Any]) -> str:
    """Tool-level (data) errors only — protocol classes are uniform and
    footnoted below the table."""
    if "trace_id" in schema.get("required", []):
        if name == "lens.get_trace":
            return "trace ausente → `{\"trace\": null}`"
        return "trace ausente → `isError` (`trace not found`)"
    return "—"


def render() -> str:
    """The full markdown document, deterministically ordered."""
    tools = sorted(_tools(), key=lambda t: t["name"])
    lines = [
        "# Referência — tools MCP (matriz gerada)",
        "",
        "<!-- GERADO por scripts/gen_mcp_matrix.py a partir do registry",
        "     real (forge_lens.mcp_server._tools) — não editar à mão.",
        "     Regenere: .venv/bin/python scripts/gen_mcp_matrix.py -->",
        "",
        f"O servidor `forge-lens mcp-serve` expõe **{len(tools)} tools**",
        "read-only sobre a store local. `*` = parâmetro obrigatório,",
        "`?` = opcional.",
        "",
        "| Tool | Descrição | Parâmetros | Limites | Erros de dados |",
        "|---|---|---|---|---|",
    ]
    for t in tools:
        schema = t.get("inputSchema", {})
        desc = " ".join(t["description"].split())  # collapse whitespace
        lines.append(
            f"| `{t['name']}` | {desc} | {_params_cell(schema)} "
            f"| {_bounds_cell(schema)} | {_errors_cell(t['name'], schema)} |"
        )
    lines += [
        "",
        "Classes de erro uniformes (protocolo, todas as tools):",
        "",
        "- argumento malformado (validação `TypeError`) → `-32602`",
        "  `invalid params`;",
        "- tool name ausente/mistipado → `-32602`; tool name desconhecido",
        "  → `result.isError` (miss de dados, não falha de protocolo);",
        "- falha interna → `-32603` opaco (`internal tool error:",
        "  {TipoDaExcecao}` — nunca o texto da exceção);",
        "- frame inválido → `-32700` (parse) / `-32600` (request) /",
        "  `-32601` (método); request pré-`initialize` → `-32602`.",
        "",
        "Contrato `isError`: qualquer payload com chave `error` de topo é",
        "retornado como `result.isError = true` — o loop nunca morre por",
        "dado faltante.",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--stdout", action="store_true",
                      help="imprime em vez de escrever o arquivo")
    mode.add_argument("--check", action="store_true",
                      help="compara com o arquivo; exit 1 se diferente")
    ap.add_argument("--out", type=Path, default=OUT_PATH,
                    help="caminho de saída (default: %(default)s)")
    ns = ap.parse_args(argv)

    doc = render()
    if ns.stdout:
        print(doc, end="")
        return 0
    if ns.check:
        current = ns.out.read_text() if ns.out.exists() else None
        if current != doc:
            print(f"{ns.out}: fora de sincronia — rode "
                  f"scripts/gen_mcp_matrix.py", file=sys.stderr)
            return 1
        return 0
    ns.out.write_text(doc)
    print(f"wrote {ns.out} ({len(doc)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
