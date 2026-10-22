# factory/ — Loop Factory layer

Product changes flow through Loop Factory specs: a spec enters
`specs/inbox/`, is dispatched to `specs/active/`, is implemented against
its acceptance criteria, and is archived under `specs/archive/` only
after `factory/quality-gate.md` passes with evidence recorded in
`factory/runs/`. Dispatch/archive normally runs via
`loop-factory dispatch --stage` / `loop-factory archive <id> --accepted`
(binary lives in the Loop-Factory repo checked out beside this one —
`../Loop-Factory/bin/loop-factory`); until then, move spec files between
`inbox/`/`active/`/`archive/` manually.
