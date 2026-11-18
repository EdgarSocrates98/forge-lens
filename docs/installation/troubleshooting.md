# Troubleshooting

## `uvicorn not installed` ao `forge-lens serve`

Extra `server` ausente → `uv sync --extra server` (ou `pip install "forge-lens[server]"`).

## `database is locked`

Outra instância escreve no mesmo `lens.db`. Rode um `serve` por DB ou use
`--db` distintos. `busy_timeout=5s` já tolera picos; lock persistente =
processo zumbi segurando WAL.

## `413 payload too large`

Ajuste `max_payload_bytes`/`max_spans_per_request` em
`.forge-lens/config.toml` — o limite existe para proteger o processo.

## `401 invalid or missing API key`

`api_key` configurada → envie `X-Forge-Lens-Key` nos POSTs de ingestão.
`GET` continua aberto.

## Spans "sumiram"

- Re-ingestão idêntica conta como `duplicate_spans` (idempotência, não perda).
- `capture=metadata` descarta corpos de prompt/tool por política —
  verifique `sanitize.dropped_content` na resposta do ingest.
- `integrity_flags` no span indicam parent ausente/truncamento.

## `doctor` falha

Leia os `[FAIL]` — cada linha diz o check e o detalhe. Comum: `data dir`
inexistente (rode `forge-lens init`) e DB corrompido (restaure do backup
ou recomece — é um arquivo único).

## `install` sai 1 com `FORGE-INSTALL-PLAN-NOT-APPROVED`

Falta a aprovação da mutação — passe `--yes` (não-interativo) ou responda
`y` na confirmação (default é **Não**; Enter vazio não instala).
`--no-input` sem `--yes` nunca aprova.

## `install` sai 1 com `FORGE-INSTALL-SCOPE-UNSUPPORTED`

`--scope user` não é superfície do lifecycle CLI — o runtime de conta
(venv, launcher, registry `~/.forge`) é instalado por
`./install.sh`/`install.ps1`. Use `--scope project` (default) ou
`workspace`.

## `install status` mostra `receipt_recorded` / `unverified`

Há um receipt em `<data-dir>/install/receipt.json` mas o estado que ele
descreve não existe (`config.toml`/`artifacts/` ausentes, ou a conta sem
manifesto/venv). Receipt é trilha de auditoria, não prova — `installed`
só aparece com estado real observado. Re-instale com `forge-lens
install --yes` ou verifique `~/.forge/installations/forge-lens.json`
(camada de conta, via `./install.sh`).

## JSON Schema fora de sincronia

`python -m forge_lens.contracts.jsonschema` regenara `schemas/v1/`; CI
falha se divergir dos modelos.
