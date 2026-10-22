# Quality Gate Checklist — forge-lens

Every active spec in `factory/specs/active/` must satisfy this checklist
before it can be archived. Evidence lives under `factory/runs/` — a gate
item without a recorded run is not done.

## Gate items

- [ ] `python -m pytest -q` — all tests pass (unit/contract run with the
      socket gate; integration/e2e keep their real loopback servers)
- [ ] `python -m ruff check .` — lint clean (line length 100)
- [ ] `python -m mypy src` — strict typecheck clean
- [ ] `uv build` — wheel + sdist build
- [ ] `python -m forge_lens.contracts.jsonschema` clean — no drift between
      `contracts/v1` and `schemas/v1/` (or regenerated files committed)
- [ ] Boundary proofs green — `tests/unit/test_boundary.py` (no network
      imports, no civil-time reads in the pure tree)
- [ ] Golden corpus green — `tests/golden/` scenarios pass; corpus
      reductions are a review decision, never an accident
- [ ] Contracts unbroken — changes to `contracts/v1` bump
      `schema_version` and keep readers of older payloads working
- [ ] Evidence-first honored — every emitted claim carries `provenance`;
      no invented observations (`null` over fabricated zeros)
- [ ] `cd packages/forge-lens-sdk-ts && npm test` — TS SDK green when it
      changed
- [ ] Version unchanged in `pyproject.toml`; no registry publish

## Record

Run records land in `factory/runs/<spec-or-gate>-<date>.json`.
