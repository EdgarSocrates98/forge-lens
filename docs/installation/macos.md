# Instalação — macOS

Rota portátil (recomendada — igual ao Linux):

```bash
./install.sh              # ~/.forge + ~/.local/bin + env.sh
./install.sh --dry-run
```

Rota dev (venv do projeto):

```bash
brew install uv                     # ou curl install script
uv sync --extra server
uv run forge-lens doctor
```

Notas:

- Apple Silicon e Intel: wheels universais das deps — sem build nativo.
- Gatekeeper: binários do venv do projeto não são assinados — primeira
  execução pode pedir permissão em Settings → Privacy & Security.
- `forge-lens serve` em `127.0.0.1` não dispara prompt de firewall.
