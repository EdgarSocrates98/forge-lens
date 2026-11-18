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
pip install "forge-lens-fl[server]"
forge-lens doctor
```

## Duas camadas de instalação (Install 2.1)

O Forge Lens é uma **Service Forge** (`installation_kind: "service"` no
`forge.json`) e tem duas camadas de instalação que nunca se confundem:

| camada | dono | o que contém | evidência |
| --- | --- | --- | --- |
| **conta** (`user`) | `scripts/forge_bootstrap.py` via `install.sh`/`install.ps1` | venv `~/.forge/installs/forge-lens`, launcher `~/.local/bin/forge-lens`, `FORGE_LENS_*` persistentes, manifesto `~/.forge/installations/forge-lens.json` + recibo `.forge-lens-bootstrap.json` | `forge/InstallationManifest/v1` |
| **projeto/workspace** | `forge-lens install` (lifecycle CLI) | `<data-dir>/` (padrão `./.forge-lens` ou `<root>/.forge-lens`): `config.toml`, `artifacts/`, recibos | `forge/InstallReceipt/v1` |

O CLI **observa** a camada de conta (`install status`/`install doctor`
leem o manifesto do registry) mas **nunca escreve** nela — venv,
launcher e registry são do bootstrap portátil. Um recibo é trilha de
auditoria, não prova de instalação: `status` só reporta
`installed: true` quando existe estado real (state dir + `config.toml`
na camada de projeto, ou runtime observado na camada de conta);
receipt sem estado vira `receipt_recorded`/`unverified`, nunca
`installed`.

## Ciclo de vida do recibo

O CLI expõe os verbos do módulo `forge_lens.install.lifecycle` —
recibos canônicos `forge/InstallReceipt/v1` em
`<data-dir>/install/receipts/<op>-<ts>.json` (histórico que o
orquestrador descobre) + `install/receipt.json` (ponteiro "latest"):

```bash
forge-lens install [--scope project|workspace] [--root DIR]
    [--profile minimal|recommended|full]
    [--source pypi|wheel|git|zip|local|gitless|offline]
    [--file artifacts/lens.db] [--host claude]
    [--components a,b] [--gateway-profile deep]
    [--dry-run] [--yes|--no-input] [--json] [--data-dir DIR]
forge-lens install status    [--root DIR|--data-dir DIR] # ou: install-status (status+doctor)
forge-lens install doctor    [--root DIR|--data-dir DIR] # exit 1 se check obrigatório falha
forge-lens install uninstall [--root DIR|--data-dir DIR] [--yes|--no-input]
```

**Gate de aprovação:** mutações exigem `--yes` (não-interativo, o que o
orquestrador envia) ou confirmação interativa com default **Não** —
Enter vazio nunca muta. `--no-input` sem `--yes` nunca aprova.
`--dry-run` é só plano: pula o gate e não escreve nada.

**Escopo:** `project` e `workspace` são aceitos (`workspace` prepara o
state dir da raiz; a enumeração de membros é do orquestrador — o recibo
marca `workspace_member_discovery: NOT_APPLICABLE`). `user` é recusado
com `FORGE-INSTALL-SCOPE-UNSUPPORTED` — é a camada do bootstrap.

**Perfis:** `minimal`/`recommended`/`full` são **equivalentes** hoje —
o lens não tem componentes instaláveis gateados por perfil (§5.4:
equivalência declarada, não diferenciação fingida). O perfil pedido é
gravado verbatim em `profile_semantics`.

**Flags sem efeito:** `--host`, `--components`, `--gateway-profile` são
aceitos para compatibilidade com a delegação do orquestrador e
registrados em `flags_recorded_not_applicable` com o motivo — nunca
silenciados, nunca fingidos em comportamento.

**Uninstall:** escopo-recibo apenas — remove os arquivos rastreados do
receipt (`config.toml` + `--file`s, containment por path resolvido) e
grava um receipt `uninstall`. O runtime de conta (venv, launcher,
manifesto do registry, `FORGE_LENS_*`) **não é removido** — a fronteira
fica explícita em `scope_boundary` no recibo.

**Saúde real:** `status` cruza as duas camadas (`project` × `account`)
e reporta `installed|degraded|receipt_recorded|unverified|
not_installed|uninstalled`; `doctor` roda checks reais (state dir
presente e gravável, `config.toml` parseável, receipt canônico,
manifesto+venv+launcher da conta — informativo: ausência vira
`UNVERIFIED`, nunca `PASS` fabricado).

## Verificar

```bash
forge-lens init /tmp/demo && cd /tmp/demo
forge-lens serve &
curl -s localhost:7777/v1/health
```

Sem `--extra server`, `forge-lens init|ingest|traces|doctor` funcionam
(SDK+CLI); `serve` reporta dependência ausente com instrução — degradado
honesto, não crash.
