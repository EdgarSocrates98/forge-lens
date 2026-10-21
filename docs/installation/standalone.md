# Instalação — Standalone

## Requisitos

- Python ≥ 3.11 (testado em 3.12/3.14)
- [uv](https://docs.astral.sh/uv/) (recomendado) ou pip
- Node ≥ 18 apenas para o SDK TypeScript

## Instalador portátil (recomendado)

Os wrappers na raiz seguem o mesmo modelo dos demais projetos Forge —
são camadas finas sobre `scripts/forge_bootstrap.py`, que faz
`discover → resolve-python → create-venv → install-runtime →
write-launcher → smoke-verify → register → report`:

```bash
# Linux / macOS
./install.sh              # instala em ~/.forge/installs/forge-lens
./install.sh --dry-run    # mostra o plano sem escrever nada
./install.sh --editable   # instala o checkout (dev) em vez do wheel

# Windows — Git Bash
bash install.windows.sh   # mesma rota POSIX, recusa shells não-bash

# Windows — PowerShell (dot-source para herdar o PATH na sessão)
. .\install.ps1
.\install.ps1 --dry-run
```

`setup.sh`/`setup.ps1` são atalhos equivalentes que invocam o bootstrap
diretamente. O instalador:

- resolve `python3`/`python`/`py` respeitando `forge.json` (`>=3.11`);
- cria um venv próprio em `~/.forge/installs/forge-lens/.venv`
  (**nunca** toca o `.venv` do projeto);
- instala o wheel construído do checkout (não depende do clone depois);
- grava o launcher em `~/.local/bin/forge-lens` (ou shim `.cmd` no
  Windows) e persiste `FORGE_LENS_*` em
  `${XDG_CONFIG_HOME:-~/.config}/forjas/env.sh` / HKCU;
- registra manifesto + recibo auditáveis em
  `~/.forge/installations/forge-lens.json` — incluindo o comando MCP
  (`forge-lens mcp-serve`) e o entrypoint `python -m forge_lens install`;
- roda `forge-lens --version` como smoke — falha se a saída vier vazia.

Depois da instalação, abra um shell novo ou rode
`source ~/.config/forjas/env.sh` (POSIX) / dot-source o `install.ps1`
(PowerShell) para herdar as variáveis na sessão.

## Do repositório (dev)

```bash
git clone <repo> && cd forge-lens
uv sync --extra server        # API+CLI; sem --extra server só SDK/CLI
uv run forge-lens doctor
```

## Via pip (quando publicado)

```bash
pip install "forge-lens[server]"
forge-lens doctor
```

## Ciclo de vida do recibo

O CLI expõe os verbos do módulo `forge_lens.install.lifecycle` —
recibos auditáveis em `<data-dir>/install/receipt.json`:

```bash
forge-lens install [--profile recommended] [--source local] \
    [--file artifacts/lens.db] [--dry-run] [--data-dir DIR]
forge-lens install status  [--data-dir DIR]   # ou: install-status (status+doctor)
forge-lens install doctor  [--data-dir DIR]   # exit 1 se algum check falha
forge-lens install uninstall [--data-dir DIR] # remove arquivos rastreados + recibo
```

## Verificar

```bash
forge-lens init /tmp/demo && cd /tmp/demo
forge-lens serve &
curl -s localhost:7777/v1/health
```

Sem `--extra server`, `forge-lens init|ingest|traces|doctor` funcionam
(SDK+CLI); `serve` reporta dependência ausente com instrução — degradado
honesto, não crash.
