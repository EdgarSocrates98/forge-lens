# Instalação — Windows

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
