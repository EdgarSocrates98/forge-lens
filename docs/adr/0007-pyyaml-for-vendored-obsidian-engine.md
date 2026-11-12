# ADR 0007 — PyYAML as a runtime dependency for the vendored Obsidian engine

- Status: accepted
- Date: 2026-11-11
- Scope: runtime dependencies, `forge_lens._forge_obsidian`

## Context

The FORJAS ecosystem federates Obsidian interoperability through the
neutral `forge_obsidian` engine (canonical source:
`product-forge/forge-obsidian`, vendored byte-parity into each forge as
`<pkg>/_forge_obsidian/`).  The engine parses Markdown frontmatter and
renders note bodies with `yaml.safe_load`/`safe_dump`, so every receiving
forge needs `PyYAML` at runtime.  Lens' rule is that new runtime deps
require a recorded ADR — this is that record.

## Decision

Add `PyYAML>=6.0` to `dependencies`.  It is already a transitive presence
across the ecosystem (api-forge, data-sentinel, doctor-api, doctor-data,
platform-forge, both spark forges); `safe_load` is the only API consumed
(no `load`, no custom loaders), which keeps the known YAML surface safe.

## Consequences

- `forge-lens obsidian …` mounts the shared seven-verb surface (status,
  scan, preview, sync-plan, export, export-note, history) with the same
  `theforge/Obsidian*/v1` envelopes the other forges emit.
- Vault access remains operator-config only (`FORGE_OBSIDIAN_VAULT` /
  `FORGE_LENS_OBSIDIAN_VAULT`); the CLI never accepts a vault path as an
  argument, and Lens' adapters keep consuming body-free receipts.
- No existing contract changes; `schemas/v1/` untouched.
