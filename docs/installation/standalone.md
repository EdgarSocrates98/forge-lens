# Instalação — Standalone

## Requisitos

- Python ≥ 3.11 (testado em 3.12/3.14)
- [uv](https://docs.astral.sh/uv/) (recomendado) ou pip
- Node ≥ 18 apenas para o SDK TypeScript

## Do repositório

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

## Verificar

```bash
forge-lens init /tmp/demo && cd /tmp/demo
forge-lens serve &
curl -s localhost:7777/v1/health
```

Sem `--extra server`, `forge-lens init|ingest|traces|doctor` funcionam
(SDK+CLI); `serve` reporta dependência ausente com instrução — degradado
honesto, não crash.
