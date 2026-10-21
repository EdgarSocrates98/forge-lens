# Instalação — Portátil / Offline

## Pacote autocontido

O wheel `forge-lens` + SQLite stdlib cobre todo o núcleo. Para ambiente
sem rede:

```bash
# numa máquina com rede:
uv build                       # dist/forge_lens-*.whl + sdist
uv pip download fastapi uvicorn typer rich pydantic -d wheels/   # deps server

# na máquina offline:
pip install --no-index --find-links wheels/ forge_lens-<ver>-py3-none-any.whl
```

## Diretório de dados portátil

Todo o estado vive em `<projeto>/.forge-lens/` (`lens.db` + `config.toml`
+ `artifacts/`). Mover/copiar o diretório = migrar a instalação.
`FORGE_LENS_DB` aponta o DB para qualquer caminho.

## Sem Docker, sem K8s, sem nuvem

Por design — local-first. Containers são opção de *deploy*, nunca requisito.
