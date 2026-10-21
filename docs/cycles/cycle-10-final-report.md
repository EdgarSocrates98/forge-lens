# Cycle 10 — Relatório Final: Ecossistema Forge, Hosts, MCP & Instalação

**Status**: `IMPLEMENTED` — matriz honesta de capacidades de host,
importer Claude Code, observer MCP stdio, lifecycle de instalação com
receipts e 7 skills reais; adapters sem superfície real ficam
`unsupported` — declarado, não escondido.
**Data**: 2026-10-21
**Entrega**: commit `bd47625` (entrega consolidada c02/07–12). Reporte
original resumido em `cycle-06-12.md`; este arquivo é o relatório
individual exigido pela spec §10.

## 1. Escopo implementado (verificável em código)

| Componente | Arquivos | Estado |
|---|---|---|
| Matriz de capacidades por host/mechanism — `unsupported` é resposta válida | `src/forge_lens/adapters/capabilities.py` → `GET /v1/capabilities` | VERIFIED |
| Importer Claude Code — sessões JSONL → `Trace` (`provenance=imported`) | `src/forge_lens/adapters/claude_code.py`, `forge-lens import-claude` | VERIFIED |
| Servidor MCP stdio — JSON-RPC `tools/list`/`tools/call` read-only (3 tools na entrega; 11 após cycle 13) | `src/forge_lens/mcp_server.py`, `forge-lens mcp-serve` | VERIFIED |
| Lifecycle de instalação — receipts, doctor, uninstall (`install-status`) | `src/forge_lens/install/lifecycle.py` | VERIFIED |
| 7 skills invocando verbos reais | `skills/forge-lens-*/SKILL.md` | VERIFIED |
| Guias de instalação por SO/perfil | `docs/installation/{linux,macos,windows,portable,standalone}.md` | VERIFIED |

## 2. Semântica honesta

- **Matriz per-mechanism**: cada host declara `native_tracing`,
  mecanismo, cobertura e limites — ninguém promete tracing completo de
  um host via logs parciais. Cursor: só tráfego MCP, sem internals.
- **Importer com fixture real**: o parser de `claude_code` cobre o
  formato real de `~/.claude/projects/*/*.jsonl` (corrigido na
  review-33 para o formato verdadeiro).
- **Observer, não interceptor**: `mcp-serve` é superfície de consulta
  read-only; instrumentação de tráfego MCP ao vivo segue
  `DOCUMENTED_ONLY` (`docs/reference/mcp.md`).
- **Standalone de verdade**: o Lens funciona sem nenhuma outra Forja —
  integração é por artefato/contrato, nunca dependência de código
  (spec §10.2).

## 3. Exclusões honestas (não implementado — deferrals declarados)

- **Adapters codex / devin / copilot**: `unsupported` com motivo na
  matriz — codex: formato de log instável; devin: sem superfície
  pública; copilot: superfície fechada. Adapters novos exigem fixture
  real verificado.
- **Instrumentação MCP ao vivo**: só onde existir ponto real de
  instrumentação (cliente/servidor/gateway) — nunca claim universal.
- **Instaladores empacotados por SO**: guias publicados; binários
  assinados por plataforma não são reivindicados.

## 4. Evidência de verificação

- `GET /v1/capabilities` serve a matriz verbatim; `lens.capabilities`
  expõe o mesmo payload via MCP (parity adicionada no cycle 13).
- 288 testes py verdes no commit de entrega; `test_mcp_tools.py` cobre
  o servidor stdio real (tools/list|call, args malformados → JSON-RPC
  error sem matar o loop); o importer `parse_session` é exercido em
  `test_ops.py` contra fixtures do formato real de sessão.
