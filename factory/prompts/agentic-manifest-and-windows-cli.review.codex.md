Review Loop Factory spec `agentic-manifest-and-windows-cli` against current working tree.

        Native adapter: `codex`
        Spec path: `E:\projetos\FORJAS\forge-lens\factory\specs\active\agentic-manifest-and-windows-cli.md`

        Review stance:
        - Findings first. Focus correctness, regressions, tests, security, maintainability.
        - Compare implementation against acceptance criteria.
        - Run or inspect verification evidence:
        - `uv run pytest -q tests/unit/test_agentic_manifest.py tests/unit/test_cli.py -k "manifest or help"`
- `uv build`
- `uv run ruff check src/forge_lens/cli/main.py tests/unit/test_agentic_manifest.py tests/unit/test_cli.py`
        - If accepted, say `ACCEPTED`.
        - If not accepted, say `CHANGES_REQUESTED` and list blocking items.
        - Do not move files. Operator or CLI archive step moves accepted specs.

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
