# Cycle 16.1 — Wave 10 (forge-lens) — run report

Spec: `factory/specs/inbox/cycle16_1-wave10-forge-lens.md` (ecosystem factory).
Branch: `cycle16_1/wave10` off `main@a84365f`. Scope: this repo only — no
sibling checkouts touched (read-only consultation of `the-forge`
contracts allowed and used).

## Delivered

### 1. `forge/InstallLifecycle/v1` — receipts/status compatible

`src/forge_lens/install/lifecycle.py` now emits the additive
`lifecycle` envelope (`{schema, state, detail}`) on:

- every `forge/InstallReceipt/v1` (install plan/apply/refusal,
  uninstall, persisted `receipts/<op>-<ts>.json` + `receipt.json`);
- `install status` and `install doctor` (`forge/InstallationHealth/v1`).

Derivation (`receipt_state`) mirrors the orchestrator's rules exactly:
`planned`→planned; `blocked|refused|unsupported`→blocked;
`completed`+`verification.PASS`→verified, +`FAIL`→failed, else→applied;
`failed`→failed unless every check is gated (→blocked);
unknown/absent→unverified. Health docs map `installed`→verified,
`degraded`→partially_applied, `not_installed|uninstalled|
receipt_recorded|unverified`→unverified; doctor `ok`→ready, real-state
failure→failed, nothing-installed→unverified. Gate-refusal receipts
(approval, `--scope user`, uninstall approval) gained a `BLOCKED`
check so they derive `blocked` — `status` stays `failed`, so existing
receipt readers and the orchestrator's `_delegation_verdict` are
unaffected. Closed 9-state vocabulary; additive — legacy receipts
still parse and derive.

### 2. Incremental reinstall — hash-compare (§7.4)

Install receipts now carry `managed_hashes` (`sha256:<hex>` per
managed file inside the data dir — same resolved-containment rule as
uninstall; paths escaping the data dir are recorded but never hashed)
and a `reinstall` delta against the prior install receipt:

`baseline` (`first_install | prior_receipt | prior_receipt_no_hashes`),
`prior_receipt_id`, `hash_compare` (`sha256 | unavailable |
not_applicable`), `unchanged/changed/added/missing/
removed_from_manifest` lists + `summary` counts,
`user_state_preserved: true` and an explicit "never rewritten" note.

Managed files are never rewritten by `install` (`ensure_state_dir`
never overwrites `config.toml`; `--file`s are declared, not written)
— so the delta *is* the proof that repeated installs are incremental.
A legacy receipt without `managed_hashes` yields a names-only delta
with `hash_compare: unavailable`, never a fake "unchanged".
`--dry-run` previews the same delta against current disk state.

### 3. Context metadata honest; minimal L0 boot

`forge.agentic.json` (root + packaged copy, byte-identical):
`skill`/`agent` resources now declare `description` (mirrored verbatim
from each asset's frontmatter `description:` — the file stays the
single source of truth), `domain: "observability"` and `tags` —
the metadata the orchestrator's L1 catalog and L2 selection score
(`context/plan.py` uses domain/tags/description). No dead fields
added (top-level `domains`/`name` are orchestrator-supplied, not
manifest-read).

`AGENTS.md` is the repo L0 — verified minimal by test (<6 KiB bound,
required sections/pointers).

### 4. Host surfaces sync verified

`tests/unit/test_lifecycle_v1.py` enforces: every agentic resource's
`location` exists in checkout and `packaged_location` exists in the
package; manifest `description` == asset frontmatter (host-facing sync
can't drift silently); `mcp_verify_tool` is a real tool in
`mcp_server._tools()`; every `host_adapters` entry has a real adapter
module; every workflow verb exists on the CLI.

### 5. `forge-lens doctor` honest health surface

Added a single observe-only line `[info] install lifecycle: <state>
(project=…, account=…)` — the verdict still lives in
`install status|doctor`; the info line never becomes a PASS/FAIL check
and degrades to `unreadable` instead of crashing the diagnosis. No
other doctor change was warranted — existing checks are already honest
(`db not created` is `[info]`, absent account layer is `UNVERIFIED`).

### 6. Docs (PT-BR canonical)

- `docs/installation/standalone.md` — `forge/InstallLifecycle/v1`
  vocabulary + `reinstall`/`managed_hashes` semantics documented.
- `docs/reference/cli.md` — install section + `forge-lens doctor`
  section updated.
- `README.md` — Cycle 16.1 status line.
- `CHANGELOG.md` — Unreleased entry.
- Changed examples run live (`install` → `reinstall` → `status` →
  `install doctor` → `doctor` in `/tmp/w10-demo`).

## Gates

| gate | result |
|---|---|
| `pytest -q` | all pass (full suite incl. 16 new tests in `tests/unit/test_lifecycle_v1.py`) |
| `ruff check .` | clean |
| `mypy src` | clean (83 files) |
| `uv build` | wheel + sdist built; `forge_lens/forge.agentic.json` verified inside the wheel with new metadata |
| `npm test` (TS SDK) | 39 pass / 0 fail |

Contract note: `contracts/v1` untouched — `forge/InstallLifecycle/v1`
is the ecosystem/orchestrator schema name (like
`forge/InstallReceipt/v1`), not a `forge_lens/*` domain contract, so
`schemas/v1/` regeneration does not apply.

## Live evidence (excerpt)

```text
install  → lifecycle.state=verified  reinstall.baseline=first_install
reinstall→ baseline=prior_receipt  hash_compare=sha256
           unchanged=[config.toml]  missing=[notes.txt]
status   → state=installed  lifecycle.state=verified
install doctor → ok=true  lifecycle.state=ready
forge-lens doctor → [info] install lifecycle: verified (project=installed, account=not_installed)
```

## Honest gaps / NO_CHANGE candidates

- `.forge/context/` self-registration (vendored `_forgecontext` +
  `install_transaction`) is NOT in forge-lens — by its install-2.1
  design the orchestrator owns context registration and discovers lens
  receipts under `<root>/.forge-lens/install/receipts/`. Vendoring a
  ~3k-line context layer would be a new subsystem — out of
  feature-freeze scope.
- `uninstall` receipts do not carry `managed_hashes` (files are
  removed — nothing to hash) and no `reinstall` delta (install-only
  concept); the `lifecycle` envelope does apply.
- Portal/Portal-UX fronts (frente G) are wave-15 scope.
