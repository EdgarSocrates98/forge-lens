# Auditoria de Release — forge-lens 0.1.0

**Ciclo**: 13.1 / `hardening-homologation` · **Data**: 2026-10-21 · **Auditor**: Release Engineering

Auditoria de homologação dos artefatos publicáveis (wheel + sdist PyPI,
tarball npm `@forge-lens/sdk`), workflows CI/release e cadeia de suprimento.
Todos os comandos abaixo foram executados de verdade; saídas são as reais.

---

## 1. Workflows (`.github/workflows/`)

### 1.1 `ci.yml` — defeitos encontrados e correções

| # | Defeito | Severidade | Correção aplicada |
|---|---------|-----------|-------------------|
| C1 | **Matriz testava 1 única versão de Python** — `uv sync` resolve o interpretador mais novo disponível; a matrix declarada "Python 3.11–3.14 testada em CI" (`docs/versioning.md`) era **falsa**. | Alta | `matrix.python: ["3.11"–"3.14"]` × 3 OS (12 jobs) via input `python-version` do `setup-uv` (forma documentada pela Astral — seta `UV_PYTHON`) |
| C2 | **`> /dev/null` no check de schema-sync quebrava em `windows-latest`** — shell default em runner Windows é `pwsh`, que não conhece `/dev/null`. | Alta (Windows) | redirect removido (stdout do gerador é só a lista de arquivos — útil no log) |
| C3 | Sem `permissions:` — `GITHUB_TOKEN` herdava o default do repo (potencialmente `write`). | Média | `permissions: contents: read` top-level |
| C4 | Job `security` rodava só `pip-audit` — ecossistema npm do pacote **publicado** (`@forge-lens/sdk`) sem auditoria. | Média | + `actions/setup-node` + `npm audit --audit-level=high` em `packages/forge-lens-sdk-ts` |
| C5 | `uv sync` sem `--locked` — drift silencioso de `uv.lock` passava. | Baixa/Média | `--locked` em todos os `uv sync` |
| C6 | Sem `timeout-minutes` — job pendurado roda até o cap de 6h. | Baixa | 10–20 min por job |
| C7 | Sem `concurrency` — pushes/PRs seguidos acumulavam runs obsoletos. | Baixa | `concurrency` por ref com `cancel-in-progress: true` |

### 1.2 `release.yml` — defeitos encontrados e correções

| # | Defeito | Severidade | Correção aplicada |
|---|---------|-----------|-------------------|
| R1 | **`uv sync --frozen` sem `--extra server` → pipeline de release MORTO.** `pytest -q` coleta `tests/integration/test_api.py` (`from fastapi.testclient import TestClient`) e mais 8 arquivos que importam fastapi incondicionalmente; sem o extra, o env não tem fastapi → collection error → `verify` sempre falha → `publish` nunca roda. Verificado: `uv sync --frozen --dry-run` desinstalaria `fastapi/uvicorn/starlette`. | **Crítica** | `uv sync --locked --extra server --all-groups` |
| R2 | `id-token: write` no nível do workflow concedia OIDC a **todos** os jobs, incluindo o que roda código arbitrário do checkout (contra a recomendação oficial PyPA: "only set id-token: write in the job that does publishing"). | Média | `id-token: write` movido para o job `publish` (`contents: read` no top) |
| R3 | SBOM gerado do env completo de dev — listava `pytest/mypy/ruff/pip-audit` como componentes da aplicação (57 itens). | Média | `uv run --locked --exact --no-dev --extra server` → env exato runtime+server (**21 componentes**, zero dev) |
| R4 | Version gate com `grep '^version'` — frágil (casa qualquer chave `version` top-level futura). | Baixa | `tomllib` no Python do próprio env |
| R5 | Sem timeouts/concurrency; retenção do artifact `dist` implícita (default 90d). | Baixa | `timeout-minutes`, `concurrency: release` **sem** cancel (nunca interromper mid-publish), `retention-days: 30` |

### 1.3 Itens verificados como corretos (sem mudança)

| Item | Estado |
|------|--------|
| Pins de actions | Todos versionados por tag/branch (`checkout@v4`, `setup-node@v4`, `setup-uv@v5`, `upload-artifact@v4`, `download-artifact@v4`, `gh-action-pypi-publish@release/v1` — padrão oficial PyPA). Dependabot cobre bumps. SHA-pinning = hardening opcional registrado como follow-up |
| Secrets | **Zero** `secrets.*`, zero credenciais hardcoded nos workflows/dependabot |
| OIDC trusted publishing | `id-token: write` + `environment: pypi` (gate humano) + sem `password:` → fluxo OIDC correto; attestations PEP 740 ativas por default na `release/v1` |
| Caching | `setup-uv enable-cache` (py) + `setup-node cache: npm` com `cache-dependency-path` correto (SDK e Studio) |
| `fail-fast: false` | presente nas matrizes |
| npm pack file list | `files: ["dist/src","README.md"]` — `dist/test` fora do tarball |

---

## 2. Artefato Python (wheel + sdist) — verificação executada

| Check | Comando | Resultado real |
|-------|---------|----------------|
| Build | `uv build` | `forge_lens-0.1.0-py3-none-any.whl` (**194.689 B**) + `forge_lens-0.1.0.tar.gz` (**160.322 B**) |
| Instalação isolada | `python3 -m venv /tmp/fl-verify` (Python **3.12.3**) + `pip install dist/…whl[server]` | OK, `pip check` → "No broken requirements found" |
| CLI entry point | `forge-lens --version` | `0.1.0` |
| `python -m` | `python -m forge_lens --version` | `0.1.0` |
| Subcomando MCP | `forge-lens mcp-serve --help` | OK (`--db`, `--help`) |
| Imports | `import forge_lens, forge_lens.sdk, forge_lens.api.app` | OK (`0.1.0`) |
| METADATA | `dist-info/METADATA` | Metadata-Version 2.4, `Requires-Python >=3.11`, `License-Expression: MIT`, deps `pydantic>=2.7, typer>=0.12, rich>=13.7` + `Provides-Extra: server` (`fastapi>=0.115, uvicorn>=0.30`) — **bate com pyproject** |
| entry_points.txt | — | `forge-lens = forge_lens.cli.main:app` ✓ |
| sdist | `tar -tzf` (104 arquivos) | `src/forge_lens/**`, `README.md`, `PKG-INFO`, `pyproject.toml` (+`pyproject.toml.orig`, artefato normal do uv_build). **Sem `LICENSE`, sem `schemas/`, sem `tests/`** |

### Defeitos fora do escopo desta auditoria (ação do coordenador)

| # | Defeito | Evidência | Correção sugerida (fora do meu escopo) |
|---|---------|-----------|----------------------------------------|
| F1 | **Texto da licença MIT não embarca** nem no wheel (`dist-info/licenses/` ausente) nem no sdist — MIT exige redistribuição do texto. | `find /tmp/fl-wheel -iname "*licen*"` → vazio | `[project] license-files = ["LICENSE"]` no pyproject — **verificado** que uv_build então grava `dist-info/licenses/LICENSE` + `LICENSE` no sdist |
| F2 | Classifiers declaram só 3.11/3.12/3.13; `docs/versioning.md` e a nova matrix declaram 3.14. | `Classifier: Programming Language :: Python :: 3.x` | adicionar `Programming Language :: Python :: 3.14` ao pyproject |
| F3 | sdist não carrega `schemas/v1/` nem `tests/`. | listagem do tarball | schemas são geradas (não lidas em runtime) → aceitável; se o mantenedor quiser o sync-check reproduzível a partir do sdist: `tool.uv.build-backend.sdist.include = ["schemas/**", "tests/**"]` — decisão registrada, não bloqueante |

---

## 3. Artefato npm (`@forge-lens/sdk`) — verificação executada

| Check | Comando | Resultado real |
|-------|---------|----------------|
| Install reproduzível | `npm ci` | `added 3 packages, audited 4` — **0 vulnerabilities** |
| Build | `npm run build` (tsc NodeNext) | limpo, sem emissão de erros |
| Testes | `npm test` | **39 testes / 10 suites, pass 39, fail 0** + `package entry OK` |
| Smoke de entry | `test/smoke-entry.mjs` | resolve o pacote via self-reference `exports` — prova que `main`/`exports` apontam para arquivos emitidos |
| Conteúdo do pacote | `npm pack --dry-run` | **20 arquivos, 23,8 kB / 91,0 kB descompactado** — `dist/src/*` (.js/.d.ts/.map) + README + package.json |
| exports map | `.` → `types` + `import` | correto para ESM consumido via `import` |
| Vazamento de tipos Node 22 | grep `node:`/`AsyncLocalStorage`/`Buffer` em `dist/src/*.d.ts` | **superfície pública .d.ts livre de tipos node:** `AsyncLocalStorage`/`randomBytes` são internos; `@types/node ^22` (devDep) não vaza tipos 22-only para consumidores |

### Correção de matriz de suporte (no escopo)

| Item | Antes | Depois | Justificativa |
|------|-------|--------|----------------|
| `engines.node` | `>=18` | `>=20` | CI testa apenas Node 20/22; Node 18 é **EOL desde 2025-04-30**; `docs/versioning.md` já declarava "TS SDK em Node 20/22". Piso declarado = piso testado |
| SDK `README.md` | sem requisito | "Requirements: Node.js ≥20" | declaração honesta |

### Defeitos fora do escopo (ação do coordenador)

| # | Defeito | Correção sugerida |
|---|---------|-------------------|
| F4 | Tarball npm **sem LICENSE** (npm auto-inclui `LICENSE` apenas se presente na raiz do pacote — `packages/forge-lens-sdk-ts/` não tem). | copiar `LICENSE` da raiz do repo para `packages/forge-lens-sdk-ts/LICENSE` (npm inclui automaticamente) |
| F5 | `apps/studio` não declara `engines` (CI pin: node 22). | app interno, não publicado — honestidade sugere `engines.node: ">=22"` ou nota; não bloqueia release |

---

## 4. Cadeia de suprimento

| Item | Estado | Evidência |
|------|--------|-----------|
| dependabot.yml | ecossistemas `uv` (/), `npm` (SDK **e** `/apps/studio` — **adicionado nesta auditoria**, antes ausente), `github-actions` (/) — semanal | `.github/dependabot.yml` |
| SBOM CycloneDX 1.5 | **válido e agora canônico**: `gen_sbom.py` normaliza purl PEP 503 (bug corrigido: `importlib_metadata` → `importlib-metadata`). Execuções reais: env do wheel isolado → 22 componentes; env simulando o release (`--exact --no-dev --extra server`) → **21 componentes, zero dev deps** | `scripts/gen_sbom.py`, `/tmp/sbom-*.json` |
| SBOM no release | gerado para `dist/sbom.json` e publicado junto aos artifacts do workflow | release.yml `verify` |
| pip-audit | `No known vulnerabilities found` | execução local 2026-10-21 |
| npm audit | `found 0 vulnerabilities` (SDK — agora gateado em CI, nível high) | `npm ci`/`npm audit` local |
| Segredos | nenhum `secrets.*`, token ou credencial nos workflows/dependabot/ISSUE_TEMPLATE | grep `.github/` |
| Licenças (metadados) | `License-Expression: MIT` no wheel METADATA; `"license": "MIT"` no package.json | verificação §2/§3 — **texto da licença pendente (F1/F4)** |

---

## 5. Matriz de suporte declarada (pós-auditoria)

| Componente | Declarado | Testado em CI (agora) |
|------------|-----------|------------------------|
| Python | `>=3.11`; classifiers 3.11–3.13 (+3.14 pendente F2) | 3.11 × ubuntu/macos/windows, 3.12 ×, 3.13 ×, 3.14 × — **12 combinações** |
| SQLite | ≥3.40 | herdado do runner/Python — não é matrixável; declarado como requisito |
| Node (SDK) | `>=20` | CI: node 20 e 22 (ubuntu) |
| Node (Studio) | não declarado (app interno — F5) | CI pin: 22 |
| Navegadores (Studio) | vitest/jsdom | matrix real Playwright = follow-up já documentado em `docs/versioning.md` |

---

## 6. Riscos residuais (não verificáveis localmente)

| Risco | Mitigação atual |
|-------|-----------------|
| Publicação PyPI real nunca executada — configuração do trusted publisher no lado PyPI e existência do nome `forge-lens` no índice são invisíveis localmente | job `publish` gated por `environment: pypi` com aprovadores; falha seria pré-upload, sem efeito colateral |
| Troca OIDC de token (GitHub→PyPI) não exercitável fora do runner | `id-token: write` corrigido e escopado; 1ª execução exige observação |
| Matriz Windows/macOS verificada por inspeção — não executada localmente (host: Linux) | correção do `/dev/null`→pwsh raciocinada; 1º run real validará |
| `attestations` (PEP 740) — default `true` na `release/v1`; falha silenciosa não pode ser descartada sem run | `print-hash: true` já loga hashes; observar 1ª publicação |
| `uv sync --locked` agora falha se `uv.lock` divergir do pyproject (proposital — drift deve quebrar CI, não passar calado) | verificado: `--locked --dry-run` limpo hoje |
| Job `studio` (`npx ng test --watch=false`) usa o builder `@angular/build:unit-test` (vitest/jsdom) — **sem** dependência de navegador no runner | consistente com "vitest/jsdom" declarado em `docs/versioning.md`; não exercitado por mim (fora de escopo) |
| SBOM/release não anexados a um GitHub Release — artifacts vivem 30d no run | se release-notes/GH-release forem desejados, é feature nova, não fix |
| npm audit gateia em `high` — vulns low/moderate em devDeps não bloqueiam | escolha pragmática; apertar para `moderate` é decisão de política |

---

## 7. Guarda de regressão

`tests/unit/test_release_consistency.py` (novo, 7 testes) trava como teste: versão
pyproject↔`__version__`, entry point do console resolvível, matrix CI ⊇ classifiers
Python declarados e ⊆ `requires-python`, piso `engines.node` == piso da matrix CI,
metadados de licença presentes, e normalização PEP 503 do purl do SBOM.

## Resumo executivo

- **1 defeito crítico eliminado** (release pipeline morto — R1), **2 altos** (C1, C2), **4 médios**, demais baixos.
- Artifacts verificados com saídas reais: wheel 194.689 B + sdist 160.322 B instaláveis e funcionais; tarball npm 23,8 kB com superfície de tipos limpa.
- Suporte declarado agora é o suporte testado: Python 3.11–3.14 × 3 OS; Node ≥20.
- Pendências de licença (F1/F4), classifier 3.14 (F2) e decisões de política (F3/F5, SHA-pinning) encaminhadas ao coordenador — todas fora do escopo exclusivo desta auditoria.
