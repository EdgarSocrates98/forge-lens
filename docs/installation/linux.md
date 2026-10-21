# Instalação — Linux

Rota portátil (recomendada — instala em `~/.forge`, launcher em
`~/.local/bin`, variáveis `FORGE_LENS_*` em
`${XDG_CONFIG_HOME:-~/.config}/forjas/env.sh`):

```bash
./install.sh              # ou: source ./install.sh p/ herdar PATH na sessão
./install.sh --dry-run    # plano sem escrever nada
./install.sh --editable   # dev: instala o checkout em vez do wheel
```

Rota dev (venv do projeto):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv
uv sync --extra server
uv run forge-lens doctor
```

Notas:

- SQLite: stdlib do Python — nada a instalar.
- WAL exige filesystem com suporte a mmap/locks (ext4/xfs/btrfs ok;
  NFS/SMB montado pode degradar — rode local).
- Firewall: o bind default `127.0.0.1` não expõe nada.
- systemd user unit (opcional, exemplo em breve em `examples/`).
