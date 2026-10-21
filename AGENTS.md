# Agent instructions — forge-lens

Local-first, evidence-first agentic observability. Read `docs/architecture/overview.md`
before structural changes.

## Commands

```bash
uv sync --extra server          # dev env (server extra for FastAPI/uvicorn)
python -m pytest -q             # tests
python -m ruff check .          # lint (line length 100)
python -m mypy src              # strict types
uv build                        # wheel + sdist
python -m forge_lens.contracts.jsonschema   # regenerate schemas/v1/*.json
cd packages/forge-lens-sdk-ts && npm test   # TS SDK (tsc + node:test)
forge-lens doctor               # install health check
```

## Hard rules

- **Evidence-first**: every emitted claim carries `provenance`
  (`observed|imported|derived|inferred|unknown`). Never promote `inferred`
  to `observed` silently. `Confidence UNKNOWN` requires explicit unknowns.
- **Deterministic core**: no wall-clock reads, no LLM calls and no network
  imports inside `core/`, `contracts/`, `otlp/`, `sanitize/`, `storage/`,
  `ingest/` — network I/O lives only in `api/` (boundary) and `sdk/exporter`.
  Sort all emitted collections.
  - *Documented exception*: `time.perf_counter`/`time.monotonic` appear in
    `core/metrics.py` (self-observability durations — never part of stored
    evidence) and as the *injectable default* clock for `RateLimiter`/
    `IngestPipeline` (production passes the default; tests inject fakes).
    Neither reads civil time nor influences persisted data.
- **Contracts are versioned**: changes to `contracts/v1` require bumping
  `schema_version` and regenerating `schemas/v1/`; never break readers of
  older payloads (unknown fields are preserved, not dropped).
- **Safe-by-default capture**: `capture=metadata` must redact credentials
  and drop prompt/tool bodies; `full` is opt-in only.
- **No invented observations**: absent counters stay `None`; absent parents
  get `MISSING_PARENT` integrity flags, never fabricated ids.
- Runtime deps stay minimal: pydantic + typer + rich (server extra adds
  fastapi + uvicorn). Do not add runtime deps without a recorded ADR.

## Layout

- `src/forge_lens/` — contracts, core, otlp, sanitize, storage, ingest, api, sdk, cli
- `schemas/v1/` — generated JSON Schemas (do not hand-edit)
- `tests/` — unit / contract / integration / e2e + `fixtures/` golden files
- `packages/forge-lens-sdk-ts/` — zero-dep TypeScript SDK
- `docs/` — PT-BR docs (EN planned); ADRs in `docs/adr/`, reports in `docs/cycles/`
