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

## JSON Schema fora de sincronia

`python -m forge_lens.contracts.jsonschema` regenara `schemas/v1/`; CI
falha se divergir dos modelos.
