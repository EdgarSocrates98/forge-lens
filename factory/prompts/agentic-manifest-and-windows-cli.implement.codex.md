You are implementing Loop Factory spec `agentic-manifest-and-windows-cli`.

        Native adapter: `codex`
        Spec path: `E:\projetos\FORJAS\forge-lens\factory\specs\active\agentic-manifest-and-windows-cli.md`
        Spec hash: `c3cbd129e3cf252f5f5362577c11344975f7045785b702b3f73b2dcd06295073`

        Operating rules:
        - Treat spec as source of truth.
        - Automate code generation and verification, not product decisions.
        - If spec is ambiguous, make smallest reversible assumption and record it in implementation notes.
        - Keep changes scoped to acceptance criteria.
        - Update living spec only for facts learned from implementation or tests.
        - Do not archive spec. Review step does that.
        - Use Codex subagents when work splits cleanly. Ask explicitly before spawning. Keep final answer terse with files changed and checks run.

        Required verification:
        - `uv run pytest -q tests/unit/test_agentic_manifest.py tests/unit/test_cli.py -k "manifest or help"`
- `uv build`
- `uv run ruff check src/forge_lens/cli/main.py tests/unit/test_agentic_manifest.py tests/unit/test_cli.py`

        Deliverables:
        1. Implement acceptance criteria.
        2. Run verification commands or explain why unavailable.
        3. Add notes under `factory/runs/` or in final response: changed files, checks, open risks.
        4. Leave spec in `factory/specs/active/` for review.

        Spec:
        ---
        # Forge Lens agentic manifest and Windows CLI safety

## Acceptance contract

- Forge Lens exposes a versioned `forge.agentic.json` with only verified
  workflows and honest host/network/credential requirements.
- Root and packaged manifests are JSON-identical and carry package version
  `0.2.0`.
- `forge-lens --help` succeeds when stdout starts in a legacy Windows code
  page such as `cp1252`.
- The wheel contains both manifests and the listed verification commands pass.

## Out of scope

- Claiming host adapters that are not implemented or verified.
- Adding cloud credentials or network requirements to the local observer.

## Evidence classification

- `CONFIRMED`: the package version is `0.2.0`, CLI entrypoint is
  `forge_lens.cli.main:main`, and MCP entrypoint is `forge-lens mcp-serve`.
- `VERIFIED_FIXED`: after manifest/package tests, Windows encoding smoke,
  wheel inspection, and lint pass.
