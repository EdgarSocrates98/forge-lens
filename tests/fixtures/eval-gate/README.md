# eval-gate CI self-check fixtures

Committed inputs for the `eval-gate` job in `.github/workflows/ci.yml` —
they prove `scripts/eval_gate.py` wires its exit codes to real evidence
instead of passing vacuously.

The job seeds a store with `scripts/seed_demo_data.py` (the same
deterministic corpus the Studio E2E suite uses) and then runs the gate
twice:

| run | cohort files | expected verdict |
| --- | --- | --- |
| pass | `baseline.json` → `candidate-pass.json` | exit 0 — all-OK traces, `pass_rate_b` = 1.0 ≥ `min_pass_rate_b` |
| fail | `baseline.json` → `candidate-fail.json` | exit 1 — cohort contains the seeded failure trace (`0b629ef8…`, `StatusCode.ERROR`), so `error_free/no_error_spans` drops to 0.5 below the 1.0 minimum |

## Trace ids

Ids are **not** arbitrary: they are `seed_demo_data.py`'s sha256-derived
ids (`tid()` = `sha256("forge-lens-e2e/trace/<name>")[:32]`), resolved
against a freshly seeded db:

- baseline: `alpha-research-1`, `alpha-research-2` (both OK)
- candidate-pass: `alpha-research-3`, `beta-task-1` (both OK)
- candidate-fail: `beta-task-1`, `beta-fail` (one ERROR)

If `seed_demo_data.py` ever changes its id derivation or trace statuses,
regenerate the ids and update these files — the gate fails closed
(unresolved ids exit 2, which the CI step asserts is *not* what
happened), so drift breaks loudly rather than silently.

`dataset.json` carries the evaluator battery in the file-based form
(`{"evaluators": [...]}`), so no stored dataset is required —
`--dataset <file>` resolves it directly.
