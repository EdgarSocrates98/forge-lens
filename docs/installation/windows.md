# Instalação — Windows

Rota portátil (recomendada — instala em `~\.forge`, shim `.cmd`, variáveis
`FORGE_LENS_*` persistidas em HKCU):

```powershell
# PowerShell — dot-source para herdar PATH na sessão atual
. .\install.ps1
.\install.ps1 --dry-run
```

```bash
# Git Bash — a rota POSIX dentro do Git Bash, nunca spawna PowerShell
bash install.windows.sh
```

Rota dev (venv do projeto):

```powershell
winget install astral-sh.uv         # ou: powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
uv sync --extra server
uv run forge-lens doctor
```

Notas:

- Caminhos: `.forge-lens/` é criado por projeto; use `FORGE_LENS_DB` para
  caminho absoluto (ex.: `C:\data\lens.db`).
- CI cobre windows-latest — testes passam; caminhos com espaços/acentos são
  tratados via `pathlib`.
- Antivírus pode segurar o primeiro `uvicorn` — espere ou adicione exceção
  ao venv do projeto.
