# Instalação — Linux

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
