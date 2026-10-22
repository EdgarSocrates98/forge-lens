#!/usr/bin/env python3
"""forge-lens eval gate — deterministic CI regression gate over stored traces.

Runs the baseline and candidate cohorts through the same
``evaluate_traces`` → ``compare_runs`` → ``apply_gate`` path as
``POST /v1/eval/compare`` and ``/v1/experiments/{id}/run``, then maps the
gate result to a CI-friendly exit code:

=====  ============================================================
0      gate passed — every declared criterion evaluated on observed
       evidence and none failed
1      regression — at least one gate failure backed by observed
       evidence (significant regression, pass-rate minimum breach,
       allowed-drop breach)
2      insufficient data — the gate could not produce an evidence-backed
       verdict: unresolved traces, unmeasured metrics, or every test
       below the significance floor. Thin data never *fails* a build —
       and never silently *passes* one either
64     usage/config error — unreadable inputs, malformed specs, a gate
       spec that asserts nothing (sysexits ``EX_USAGE``)
70     internal error — an unexpected engine fault (sysexits
       ``EX_SOFTWARE``); the partial report still goes to stdout
=====  ============================================================

Inputs
------

``--dataset`` resolves the evaluator battery, two ways:

* a JSON **file** — ``{"evaluators": [{name, params?}...],
  "dataset_id"?, "dataset_version"?}`` or a bare ``[{name, params?}...]``
  list;
* a stored **dataset id** ``name`` or ``name@version`` — the evaluator
  specs are read from that record's ``metadata.evaluators`` (a
  documented convention of this tool: the dataset version pins both the
  truth and the battery that checks it — the engine itself never
  invents a case→spec mapping).

``--baseline``/``--candidate`` are cohort specs — inline JSON or
``@file``: ``{"trace_ids": ["<32-hex>", ...], "label"?, "project_id"?,
"dataset"?}`` or a bare ``["<trace-id>", ...]`` list (1..500 ids, the
same bound the API enforces). Every id must resolve to a stored trace —
a missing trace is unverifiable evidence, so the run reports
insufficient-data instead of comparing a partial cohort.

``--gate`` is the ``apply_gate`` spec — inline JSON or ``@file``:
``{"metrics": {"<evaluator>/<metric>": {"min_pass_rate_b"?,
"max_drop"?}}, "fail_on_significant_regression"?: bool}``.
Default when omitted: ``{"fail_on_significant_regression": true}``.

Output: the full JSON report goes to **stdout** (pipeable);
a human summary goes to **stderr**. The report is deterministic — no
wall-clock anywhere in the eval path, and every emitted collection is
sorted.

Example
-------

.. code-block:: bash

    python scripts/eval_gate.py \\
        --db .forge-lens/lens.db \\
        --dataset specs/eval-suite.json \\
        --baseline cohorts/base.json \\
        --candidate '{"trace_ids": ["abcd…"]}' \\
        --gate '{"metrics":
                  {"error_free/no_error_spans": {"max_drop": 0.05}},
                 "fail_on_significant_regression": true}'

See ``docs/eval-gate.md`` for the full contract and the GitHub Actions
workflow example.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn

if TYPE_CHECKING:
    from forge_lens.contracts.v1.trace import Trace
    from forge_lens.storage.sqlite import SQLiteTraceStore

_EXIT_PASS = 0
_EXIT_REGRESSION = 1
_EXIT_INSUFFICIENT = 2
_EXIT_USAGE = 64
_EXIT_INTERNAL = 70

_MAX_TRACE_IDS = 500
_MAX_EVAL_SPECS = 100

_SCHEMA = "forge-lens/eval-gate@1"

# failure ``reason`` strings emitted by forge_lens.eval.compare.apply_gate —
# pinned by tests in tests/unit/test_eval_stats_audit.py so a wording
# drift there breaks loudly instead of silently misclassifying here
_REASON_METRICS_NOT_OBJECT = "gate.metrics must be an object"
_REASON_SPEC_NOT_OBJECT = "metric gate spec must be an object"
_REASON_NOT_EVALUATED = (
    "metric not evaluated — cannot gate on unmeasured evidence"
)
_REASON_BELOW_MIN = "pass_rate_b below minimum"
_REASON_DROP_UNMEASURED = (
    "max_drop unmeasured — pass rate missing on one or both sides"
)

_USAGE_FAILURE_REASONS = frozenset(
    {_REASON_METRICS_NOT_OBJECT, _REASON_SPEC_NOT_OBJECT}
)

# "not_computable" is deliberately NOT thin: var==0 only happens when
# both sides observed identical samples (z: same extreme proportion;
# MWU: every pooled score tied) — that is proven equality, i.e. real
# evidence of *no* regression, not missing evidence
_SIGNIFICANCE_THIN = frozenset({"insufficient_data", "not_tested"})


class _Parser(argparse.ArgumentParser):
    """argparse with the conventional exit-2 usage error remapped to 64 —
    in this tool 2 means *insufficient data*, a data verdict, not a
    command-line mistake."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(_EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _usage_error(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(_EXIT_USAGE)


def _load_json_arg(value: str, what: str) -> Any:
    """Inline-JSON or ``@file``/path argument → parsed JSON value.

    Accepts, in order: ``@path`` (always a file), ``{...}``/``[...]``
    (inline JSON), an existing filesystem path (file), or a bare JSON
    literal. Ambiguity-free inputs only — a value that is neither valid
    JSON nor an existing file is a usage error."""
    if value.startswith("@"):
        return _load_json_file(Path(value[1:]), what)
    stripped = value.lstrip()
    if stripped.startswith(("{", "[")):
        return _parse_json_text(value, what)
    path = Path(value)
    if path.is_file():
        return _load_json_file(path, what)
    return _parse_json_text(value, what)


def _load_json_file(path: Path, what: str) -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        _usage_error(f"{what}: cannot read {path}: {exc}")
    return _parse_json_text(raw, f"{what} ({path})")


def _parse_json_text(raw: str, what: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError as exc:
        _usage_error(f"{what}: invalid JSON — {exc}")


def _load_dataset(
    store: SQLiteTraceStore, value: str, project_id: str
) -> tuple[list[Any], dict[str, Any]]:
    """Resolve ``--dataset`` → ``(evaluator specs, dataset info)``.

    Files carry their own ``evaluators``; stored datasets pin them under
    ``metadata.evaluators`` (this tool's documented convention — the
    engine itself never invents a case→spec mapping)."""
    info: dict[str, Any] = {"source": None, "ref": value}
    specs: Any
    if value.startswith("@") or Path(value).is_file():
        raw = _load_json_arg(value, "dataset")
        info["source"] = "file"
        if isinstance(raw, list):
            specs = raw
        elif isinstance(raw, dict):
            specs = raw.get("evaluators")
            info["id"] = raw.get("dataset_id") or raw.get("id")
            info["version"] = (
                raw.get("dataset_version") or raw.get("version")
            )
        else:
            _usage_error(
                "dataset: file must contain a JSON object or a list of "
                "evaluator specs"
            )
    else:
        info["source"] = "store"
        ds_id: str
        version: str | None
        if "@" in value:
            ds_id, version = value.rsplit("@", 1)
        else:
            ds_id, version = value, None
        info["id"], info["version"] = ds_id, version
        dataset = store.get_dataset(ds_id, project_id)
        if dataset is None:
            _usage_error(
                f"dataset: {ds_id!r} not found in project {project_id!r} "
                "(checked as a stored dataset id — pass a JSON file "
                "path or @file for file-based specs)"
            )
        if version is not None:
            dv = store.get_dataset_version(ds_id, version, project_id)
            if dv is None:
                _usage_error(
                    f"dataset: {ds_id!r}@{version!r} not found in "
                    f"project {project_id!r}"
                )
            specs = (dv.get("metadata") or {}).get("evaluators")
            origin = (
                f"dataset version {ds_id}@{version} metadata.evaluators"
            )
        else:
            specs = (dataset.get("metadata") or {}).get("evaluators")
            origin = f"dataset {ds_id} metadata.evaluators"
        if specs is None:
            _usage_error(
                f"{origin} is absent — a stored dataset pins its eval "
                "battery under metadata.evaluators "
                "([{'name': ..., 'params': {...}}, ...]); for file-based "
                "specs pass the file path instead"
            )
    if (
        not isinstance(specs, list)
        or not specs
        or len(specs) > _MAX_EVAL_SPECS
    ):
        _usage_error(
            "dataset: evaluators must be a list of "
            f"1..{_MAX_EVAL_SPECS} specs"
        )
    label = info.get("id") or None
    if info.get("version"):
        label = f"{label}@{info['version']}" if label else None
    info["label"] = label
    return list(specs), info


def _load_cohort(
    store: SQLiteTraceStore,
    value: str,
    side: str,
    default_project: str,
) -> dict[str, Any]:
    """Resolve a cohort spec → ids, project, label, resolved traces.

    Trace ids are resolved eagerly and *all* misses are reported — a
    cohort that references evaporated evidence is insufficient data,
    never a partial comparison."""
    raw = _load_json_arg(value, f"{side} cohort")
    if isinstance(raw, list):
        spec: dict[str, Any] = {"trace_ids": raw}
    elif isinstance(raw, dict):
        spec = raw
    else:
        _usage_error(
            f"{side}: cohort spec must be a JSON object or a list of "
            "trace ids"
        )
    ids = spec.get("trace_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > _MAX_TRACE_IDS
        or not all(isinstance(t, str) and t for t in ids)
    ):
        _usage_error(
            f"{side}: trace_ids must be a list of 1..{_MAX_TRACE_IDS} "
            "non-empty strings"
        )
    pid = spec.get("project_id")
    if pid is not None and not isinstance(pid, str):
        _usage_error(f"{side}: project_id must be a string")
    project_id = pid or default_project
    label = spec.get("label")
    if label is not None and not isinstance(label, str):
        _usage_error(f"{side}: label must be a string")
    dataset_label = spec.get("dataset")
    if dataset_label is not None and not isinstance(dataset_label, str):
        _usage_error(f"{side}: dataset must be a string")

    traces: list[Trace] = []
    missing: list[str] = []
    for tid in ids:
        trace = store.get_trace(tid, project_id)
        if trace is None:
            missing.append(tid)
        else:
            traces.append(trace)
    return {
        "label": label or side,
        "project_id": project_id,
        "dataset": dataset_label,
        "trace_ids": sorted(set(ids)),
        "traces": traces,
        "missing_trace_ids": sorted(missing),
    }


def _validate_gate(raw: Any) -> dict[str, Any]:
    """Shape-check the gate spec before ``apply_gate`` — a gate that
    asserts nothing is a pipeline misconfiguration, not a pass."""
    if not isinstance(raw, dict):
        _usage_error("gate: spec must be a JSON object")
    if "metrics" in raw and not isinstance(raw["metrics"], dict):
        _usage_error("gate: metrics must be an object")
    fosr = raw.get("fail_on_significant_regression")
    if fosr is not None and not isinstance(fosr, bool):
        _usage_error("gate: fail_on_significant_regression must be a bool")
    metrics = raw.get("metrics") or {}
    if not metrics and fosr is not True:
        _usage_error(
            "gate: spec declares no criteria — provide "
            "metrics.<evaluator>/<metric> rules and/or "
            "fail_on_significant_regression: true"
        )
    return raw


def _classify(
    compare: dict[str, Any],
    gate_out: dict[str, Any],
    gate: dict[str, Any],
) -> tuple[int, str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Map gate failures to an exit verdict.

    Returns ``(exit_code, status, evidence_failures, missing_failures)``.
    A failure only blocks (exit 1) when it is backed by observed numbers;
    criteria that could not be evaluated at all are insufficient data."""
    failures: list[dict[str, Any]] = gate_out.get("failures", [])
    bad_spec = [
        f for f in failures if f.get("reason") in _USAGE_FAILURE_REASONS
    ]
    if bad_spec:
        _usage_error(
            "gate: malformed spec — "
            + json.dumps(bad_spec, sort_keys=True)
        )
    evidence_failures: list[dict[str, Any]] = []
    missing_failures: list[dict[str, Any]] = []
    for f in failures:
        if f.get("reason") == _REASON_NOT_EVALUATED or (
            f.get("reason") in {_REASON_BELOW_MIN, _REASON_DROP_UNMEASURED}
            and f.get("observed") is None
        ):
            missing_failures.append(f)
        else:
            evidence_failures.append(f)
    if evidence_failures:
        return (
            _EXIT_REGRESSION,
            "regression",
            evidence_failures,
            missing_failures,
        )
    if missing_failures:
        return (
            _EXIT_INSUFFICIENT,
            "insufficient_data",
            evidence_failures,
            missing_failures,
        )
    # gate_passed — but a pass is only honest when something was actually
    # evaluable. Metrics criteria that passed necessarily observed real
    # values (a null pass rate would have failed), so the remaining
    # vacuous-pass risk is a fail_on_significant_regression-only spec in
    # which no test ever ran.
    if gate.get("metrics"):
        return _EXIT_PASS, "pass", [], []
    tested = any(
        d.get("p_value") is not None
        or d.get("significance") == "not_computable"  # proven equality
        for d in compare["deltas"]
    )
    if tested:
        return _EXIT_PASS, "pass", [], []
    return _EXIT_INSUFFICIENT, "insufficient_data", [], []


def _thin_metrics(compare: dict[str, Any]) -> list[str]:
    """Metrics whose significance test could not run — surfaced as
    warnings regardless of the verdict, so a green gate on thin data is
    visibly thin."""
    return sorted(
        f"{d['evaluator']}/{d['metric']}"
        for d in compare.get("deltas", [])
        if d.get("significance") in _SIGNIFICANCE_THIN
    )


def _human_report(report: dict[str, Any]) -> str:
    """Deterministic plain-text summary for CI logs (stderr)."""
    lines: list[str] = []
    v = report["verdict"]
    lines.append(
        f"eval-gate: {v['status']} (exit {v['exit_code']}) — {v['reason']}"
    )
    ds = report["dataset"]
    lines.append(
        f"dataset: {ds.get('label') or ds['ref']} "
        f"(source={ds['source']}, specs={len(report['evaluator_specs'])})"
    )
    for side in ("baseline", "candidate"):
        c = report[side]
        miss = (
            f" MISSING {len(c['missing_trace_ids'])}"
            if c["missing_trace_ids"]
            else ""
        )
        lines.append(
            f"{side}: {c['resolved']}/{c['requested']} traces "
            f"project={c['project_id']}{miss}"
        )
    compare = report.get("compare")
    if compare:
        for d in compare.get("deltas", []):
            pa, pb = d.get("pass_rate_a"), d.get("pass_rate_b")
            if pa is not None and pb is not None:
                rates = f"pass_rate {pa:g}→{pb:g}"
            else:
                rates = f"mean {d.get('mean_a')}→{d.get('mean_b')}"
            pv = d.get("p_value")
            lines.append(
                f"  {d['evaluator']}/{d['metric']}: {rates} "
                f"delta={d.get('delta')} sig={d.get('significance')}"
                + (f" p={pv:g}" if pv is not None else "")
            )
        if compare.get("multiple_comparisons"):
            lines.append(f"  note: {compare['multiple_comparisons']}")
    for f in report["verdict"]["evidence_failures"]:
        extra = {
            k: x for k, x in f.items() if k not in ("metric", "reason")
        }
        lines.append(
            f"  FAIL {f.get('metric')}: {f.get('reason')} "
            + json.dumps(extra, sort_keys=True, default=str)
        )
    for f in report["verdict"]["missing_evidence_failures"]:
        lines.append(f"  THIN {f.get('metric')}: {f.get('reason')}")
    for m in report["warnings"]:
        lines.append(f"  warn: {m}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    p = _Parser(
        prog="eval_gate",
        description=(
            "Deterministic CI regression gate over stored forge-lens "
            "traces — same compare/apply_gate path as the API."
        ),
    )
    p.add_argument(
        "--dataset",
        required=True,
        metavar="ID[@VERSION]|FILE|@FILE",
        help="evaluator battery: stored dataset id (specs from "
        "metadata.evaluators) or a JSON spec file",
    )
    p.add_argument(
        "--baseline",
        required=True,
        metavar="SPEC|@FILE",
        help="baseline cohort: {trace_ids, label?, project_id?, dataset?} "
        "or a bare trace-id list",
    )
    p.add_argument(
        "--candidate",
        required=True,
        metavar="SPEC|@FILE",
        help="candidate cohort — same shape as --baseline",
    )
    p.add_argument(
        "--db",
        metavar="PATH",
        help="sqlite store path (default: resolved forge-lens config)",
    )
    p.add_argument(
        "--gate",
        metavar="JSON|@FILE",
        help="apply_gate spec (default: "
        '{"fail_on_significant_regression": true})',
    )
    p.add_argument(
        "--project",
        metavar="ID",
        help="default project for cohorts and dataset lookup",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    """CLI entry — returns the exit code (see module docstring)."""
    # imported here so `--help` and argument errors never pay import cost
    from forge_lens.core.config import load_config
    from forge_lens.eval import compare_runs, evaluate_traces
    from forge_lens.eval.compare import apply_gate
    from forge_lens.storage.sqlite import SQLiteTraceStore

    args = _build_parser().parse_args(argv)
    try:
        cfg = load_config()
    except ValueError as exc:
        # malformed config.toml / FORGE_LENS_* — a usage problem (64),
        # never a traceback that CI would misread as a regression
        _usage_error(f"config: {exc}")
    default_project = args.project or cfg.project_id
    db_path = Path(args.db) if args.db else cfg.resolved_db_path

    try:
        store = SQLiteTraceStore(db_path, readonly=True)
    except sqlite3.Error as exc:
        _usage_error(f"db: cannot open {db_path} read-only — {exc}")

    try:
        specs, ds_info = _load_dataset(store, args.dataset, default_project)
        baseline = _load_cohort(
            store, args.baseline, "baseline", default_project
        )
        candidate = _load_cohort(
            store, args.candidate, "candidate", default_project
        )
    finally:
        store.close()

    gate = (
        _validate_gate(_load_json_arg(args.gate, "gate"))
        if args.gate is not None
        else {"fail_on_significant_regression": True}
    )

    report: dict[str, Any] = {
        "schema": _SCHEMA,
        "dataset": ds_info,
        "evaluator_specs": specs,
        "baseline": {
            k: v for k, v in baseline.items() if k != "traces"
        },
        "candidate": {
            k: v for k, v in candidate.items() if k != "traces"
        },
        "compare": None,
        "gate": None,
        "warnings": [],
        "verdict": {},
    }
    for c in (report["baseline"], report["candidate"]):
        c["requested"] = len(c["trace_ids"])
        c["resolved"] = c["requested"] - len(c["missing_trace_ids"])

    missing = baseline["missing_trace_ids"] or candidate["missing_trace_ids"]
    if missing:
        report["verdict"] = {
            "status": "insufficient_data",
            "exit_code": _EXIT_INSUFFICIENT,
            "reason": (
                "cohort trace ids did not resolve — evaporated evidence "
                "is unverifiable, never a partial comparison"
            ),
            "evidence_failures": [],
            "missing_evidence_failures": [],
        }
    else:
        try:
            compare = compare_runs(
                evaluate_traces(
                    baseline["traces"],
                    specs,
                    dataset=baseline["dataset"] or ds_info["label"],
                ),
                evaluate_traces(
                    candidate["traces"],
                    specs,
                    dataset=candidate["dataset"] or ds_info["label"],
                ),
            )
        except Exception as exc:  # an engine fault is not a data verdict
            report["verdict"] = {
                "status": "internal_error",
                "exit_code": _EXIT_INTERNAL,
                "reason": f"{type(exc).__name__}: {exc}",
                "evidence_failures": [],
                "missing_evidence_failures": [],
            }
            print(_human_report(report), file=sys.stderr)
            print(
                json.dumps(report, indent=2, sort_keys=True, default=str)
            )
            return _EXIT_INTERNAL
        try:
            gate_out = apply_gate(compare, gate)
        except (ValueError, TypeError) as exc:
            # apply_gate's authored raises are gate-spec validation —
            # a malformed criterion is usage (64). Spec errors inside
            # evaluate_traces never reach here: run_evaluators reports
            # them as evaluator_error results instead of raising.
            _usage_error(f"gate: {exc}")
        except Exception as exc:  # an engine fault is not a data verdict
            report["verdict"] = {
                "status": "internal_error",
                "exit_code": _EXIT_INTERNAL,
                "reason": f"{type(exc).__name__}: {exc}",
                "evidence_failures": [],
                "missing_evidence_failures": [],
            }
            print(_human_report(report), file=sys.stderr)
            print(
                json.dumps(report, indent=2, sort_keys=True, default=str)
            )
            return _EXIT_INTERNAL
        report["compare"] = compare
        report["gate"] = gate_out
        code, status, ev_fail, missing_fail = _classify(
            compare, gate_out, gate
        )
        report["warnings"] = [
            f"{m}: significance untested (thin data)"
            for m in _thin_metrics(compare)
        ]
        reason = {
            _EXIT_PASS: (
                "all gate criteria evaluated on observed evidence"
            ),
            _EXIT_REGRESSION: (
                "gate failures backed by observed evidence"
            ),
            _EXIT_INSUFFICIENT: (
                "no gate failure, but no criterion produced an "
                "evidence-backed verdict either (thin data)"
            ),
        }[code]
        if missing_fail and code != _EXIT_INSUFFICIENT:
            reason += "; unmeasurable criteria also present"
        report["verdict"] = {
            "status": status,
            "exit_code": code,
            "reason": reason,
            "evidence_failures": ev_fail,
            "missing_evidence_failures": missing_fail,
        }

    # stdout = the machine report (deterministic JSON); stderr = humans
    print(_human_report(report), file=sys.stderr)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return int(report["verdict"]["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
