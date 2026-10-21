"""Golden scenario corpus — declarative, deterministic end-to-end specs.

Each file under ``tests/golden/scenarios/*.json`` is a self-contained
scenario: optional ``setup`` (config, API keys, seeded data), one
``input`` operation (HTTP via TestClient, a pipeline ingest, an engine
function on a stored trace, or MCP stdio frames), plus ``expected`` /
``forbidden`` assertions over both the operation output and the
resulting store state.

The corpus itself is schema-validated: a malformed scenario file fails
the suite loudly, naming the file and the offending key. Scenarios
document *real* behavior — when the code changes semantics, the golden
changes only after a human confirms the new truth.

Format spec: ``docs/reference/golden-scenarios.md``.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from forge_lens.contracts.v1.trace import Trace
from forge_lens.core.config import CaptureLevel, LensConfig
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.security.keys import hash_secret
from forge_lens.storage.sqlite import SQLiteTraceStore

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

# Below this count the corpus is assumed damaged (files lost), not
# legitimately smaller — shrinking is a review decision, not an accident.
MIN_SCENARIOS = 30

CATEGORIES = {
    "ingest",
    "dedupe",
    "sanitize",
    "integrity",
    "limits",
    "tenancy",
    "roles",
    "rollups",
    "ops",
    "eval",
    "datasets",
    "retention",
    "audit",
    "mcp",
    "replay",
    "graph",
}

_TOP_KEYS = {
    "scenario_id",
    "title",
    "category",
    "input",
    "expected",
    "forbidden",
    "notes",
    "setup",
    "provenance",
    "evidence_required",
}
_REQUIRED_TOP = {
    "scenario_id",
    "title",
    "category",
    "input",
    "expected",
    "forbidden",
    "notes",
}
_SETUP_KEYS = {"config", "api_keys", "store_traces", "ingest_traces", "requests"}
_API_KEY_KEYS = {"key_id", "name", "secret", "role", "projects"}
_CONFIG_KEYS = {
    "project_id",
    "capture",
    "api_key",
    "auth_reads",
    "auth_mode",
    "retention_days",
    "max_payload_bytes",
    "max_traces_per_request",
    "max_spans_per_request",
    "max_events_per_span",
    "max_attr_value_chars",
    "rate_limit_per_minute",
}
_REQUEST_KEYS = {"method", "path", "headers", "params", "json", "content"}
_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}
_ENGINE_FUNCTIONS = {
    "detect_loops",
    "classify_failure",
    "replay_timeline",
    "build_trace_graph",
    "inspect_trace",
    "trace_efficiency",
}
_EXPECTED_KEYS = {
    "status",
    "statuses",
    "responses",
    "json",
    "json_contains",
    "body_contains",
    "result",
    "results",
    "error",
    "output",
    "output_contains",
    "store",
}
_RESPONSE_KEYS = {"status", "json", "json_contains", "body_contains"}
_STORE_KEYS = {
    "trace_count",
    "span_count",
    "traces",
    "spans",
    "span_flags",
    "span_flag_absent",
    "span_attrs",
    "sessions",
    "tools",
    "audit",
}
_FORBIDDEN_KEYS = {
    "body_not_contains",
    "output_not_contains",
    "store_json_not_contains",
    "audit_not_contains",
    "trace_ids_absent",
    "status_not",
}
_ERROR_KEYS = {"type", "contains"}

# Keys that carry the evidence a claim links to — used by
# ``evidence_required``: every dict bearing one must hold it non-empty.
_EVIDENCE_KEYS = ("span_ids", "evidence", "details", "observations")


class ScenarioError(Exception):
    """Malformed scenario file — a corpus bug, not a product bug."""


def _err(path: Path, msg: str) -> ScenarioError:
    return ScenarioError(f"{path.name}: {msg}")


def _unknown(path: Path, mapping: dict[str, Any], allowed: set[str], where: str) -> None:
    extra = sorted(set(mapping) - allowed)
    if extra:
        raise _err(path, f"unknown {where} key(s): {extra}")


def _validate_request(path: Path, req: Any, i: int) -> None:
    if not isinstance(req, dict):
        raise _err(path, f"requests[{i}] must be an object")
    _unknown(path, req, _REQUEST_KEYS, f"requests[{i}]")
    if req.get("method") not in _HTTP_METHODS:
        raise _err(path, f"requests[{i}].method must be one of {sorted(_HTTP_METHODS)}")
    if not isinstance(req.get("path"), str) or not req["path"].startswith("/"):
        raise _err(path, f"requests[{i}].path must be an absolute path string")
    for opt in ("headers", "params", "json"):
        if opt in req and not isinstance(req[opt], (dict, list)):
            raise _err(path, f"requests[{i}].{opt} must be an object/list")
    if "content" in req and not isinstance(req["content"], str):
        raise _err(path, f"requests[{i}].content must be a string")


def _validate_input(path: Path, inp: Any) -> str:
    if not isinstance(inp, dict):
        raise _err(path, "input must be an object")
    op = inp.get("op")
    if op == "http":
        _unknown(path, inp, {"op", "requests"}, "input")
        reqs = inp.get("requests")
        if not isinstance(reqs, list) or not reqs:
            raise _err(path, "input.requests must be a non-empty list")
        for i, req in enumerate(reqs):
            _validate_request(path, req, i)
    elif op == "pipeline":
        _unknown(path, inp, {"op", "traces", "payload_bytes", "times"}, "input")
        if not isinstance(inp.get("traces"), list) or not inp["traces"]:
            raise _err(path, "input.traces must be a non-empty list of Trace objects")
        if "times" in inp and (not isinstance(inp["times"], int) or inp["times"] < 1):
            raise _err(path, "input.times must be an integer >= 1")
        if "payload_bytes" in inp and not isinstance(inp["payload_bytes"], int):
            raise _err(path, "input.payload_bytes must be an integer")
    elif op == "engine":
        _unknown(
            path, inp, {"op", "function", "trace_id", "project_id", "params"}, "input"
        )
        if inp.get("function") not in _ENGINE_FUNCTIONS:
            raise _err(
                path, f"input.function must be one of {sorted(_ENGINE_FUNCTIONS)}"
            )
        if not isinstance(inp.get("trace_id"), str):
            raise _err(path, "input.trace_id must be a string")
        if "params" in inp and not isinstance(inp["params"], dict):
            raise _err(path, "input.params must be an object")
    elif op == "mcp":
        _unknown(path, inp, {"op", "frames"}, "input")
        frames = inp.get("frames")
        if (
            not isinstance(frames, list)
            or not frames
            or any(not isinstance(f, dict) for f in frames)
        ):
            raise _err(path, "input.frames must be a non-empty list of JSON-RPC objects")
    else:
        raise _err(path, f"input.op must be one of http|pipeline|engine|mcp, got {op!r}")
    return op


def _validate(path: Path, data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise _err(path, "top level must be a JSON object")
    _unknown(path, data, _TOP_KEYS, "top-level")
    missing = sorted(_REQUIRED_TOP - set(data))
    if missing:
        raise _err(path, f"missing required key(s): {missing}")
    if not isinstance(data["scenario_id"], str) or not data["scenario_id"]:
        raise _err(path, "scenario_id must be a non-empty string")
    if data["scenario_id"] != path.stem:
        raise _err(
            path,
            f"scenario_id {data['scenario_id']!r} must equal filename stem "
            f"{path.stem!r}",
        )
    if data["category"] not in CATEGORIES:
        raise _err(path, f"category must be one of {sorted(CATEGORIES)}")
    for name in ("title", "notes"):
        if not isinstance(data[name], str) or not data[name]:
            raise _err(path, f"{name} must be a non-empty string")
    if "provenance" in data and not isinstance(data["provenance"], str):
        raise _err(path, "provenance must be a string")
    if "evidence_required" in data and not isinstance(data["evidence_required"], bool):
        raise _err(path, "evidence_required must be a boolean")

    setup = data.get("setup", {})
    if not isinstance(setup, dict):
        raise _err(path, "setup must be an object")
    _unknown(path, setup, _SETUP_KEYS, "setup")
    if "config" in setup:
        if not isinstance(setup["config"], dict):
            raise _err(path, "setup.config must be an object")
        _unknown(path, setup["config"], _CONFIG_KEYS, "setup.config")
    for key in ("api_keys",):
        for i, k in enumerate(setup.get(key, [])):
            if not isinstance(k, dict):
                raise _err(path, f"setup.{key}[{i}] must be an object")
            _unknown(path, k, _API_KEY_KEYS, f"setup.{key}[{i}]")
            for req in ("key_id", "name", "secret", "role"):
                if not isinstance(k.get(req), str) or not k[req]:
                    raise _err(path, f"setup.{key}[{i}].{req} must be a string")
    for key in ("store_traces", "ingest_traces"):
        if key in setup and (
            not isinstance(setup[key], list)
            or any(not isinstance(t, dict) for t in setup[key])
        ):
            raise _err(path, f"setup.{key} must be a list of Trace objects")
    for i, req in enumerate(setup.get("requests", [])):
        _validate_request(path, req, i)

    op = _validate_input(path, data["input"])

    exp = data["expected"]
    if not isinstance(exp, dict):
        raise _err(path, "expected must be an object")
    _unknown(path, exp, _EXPECTED_KEYS, "expected")
    if "responses" in exp:
        if op != "http":
            raise _err(path, "expected.responses only applies to op=http")
        if not isinstance(exp["responses"], list):
            raise _err(path, "expected.responses must be a list")
        for i, r in enumerate(exp["responses"]):
            if not isinstance(r, dict):
                raise _err(path, f"expected.responses[{i}] must be an object")
            _unknown(path, r, _RESPONSE_KEYS, f"expected.responses[{i}]")
    if "store" in exp:
        if not isinstance(exp["store"], dict):
            raise _err(path, "expected.store must be an object")
        _unknown(path, exp["store"], _STORE_KEYS, "expected.store")
    if "error" in exp:
        if op != "pipeline":
            raise _err(path, "expected.error only applies to op=pipeline")
        if not isinstance(exp["error"], dict):
            raise _err(path, "expected.error must be an object")
        _unknown(path, exp["error"], _ERROR_KEYS, "expected.error")
    if ("result" in exp or "results" in exp) and op != "pipeline":
        raise _err(path, "expected.result(s) only applies to op=pipeline")

    forbidden = data["forbidden"]
    if not isinstance(forbidden, dict):
        raise _err(path, "forbidden must be an object")
    _unknown(path, forbidden, _FORBIDDEN_KEYS, "forbidden")
    return data


# -- subset / containment matchers ------------------------------------------


def _subset(expected: Any, actual: Any, where: str) -> list[str]:
    """Deep subset match — returns a list of mismatches (empty = match).

    Dicts: every expected key present and recursively matching. Lists:
    same length, element-wise. Scalars: equality.
    """
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{where}: expected object, got {type(actual).__name__}"]
        out: list[str] = []
        for k, v in expected.items():
            if k not in actual:
                out.append(f"{where}.{k}: key absent in actual")
            else:
                out.extend(_subset(v, actual[k], f"{where}.{k}"))
        return out
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return [f"{where}: expected list, got {type(actual).__name__}"]
        if len(actual) != len(expected):
            return [
                f"{where}: expected {len(expected)} items, got {len(actual)}"
            ]
        out = []
        for i, (e, a) in enumerate(zip(expected, actual, strict=True)):
            out.extend(_subset(e, a, f"{where}[{i}]"))
        return out
    if expected != actual:
        return [f"{where}: expected {expected!r}, got {actual!r}"]
    return []


def _resolve_path(obj: Any, path: str) -> Any:
    """Resolve ``a.b.0.c`` — numeric segments index into lists."""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError) as exc:
                raise KeyError(path) from exc
        elif isinstance(cur, dict):
            cur = cur[seg]
        else:
            raise KeyError(path) from None
    return cur


def _assert_contains(
    mapping: dict[str, Any], payload: Any, where: str
) -> list[str]:
    """Each expected substring must appear inside the resolved value."""
    out: list[str] = []
    for path, needles in mapping.items():
        try:
            resolved = _resolve_path(payload, path)
        except (KeyError, TypeError):
            out.append(f"{where}: path {path!r} absent in payload")
            continue
        hay = json.dumps(resolved, sort_keys=True, default=str)
        if isinstance(needles, str):
            needles = [needles]
        for n in needles:
            if n not in hay:
                out.append(f"{where}: {n!r} absent at path {path!r}")
    return out


def _walk_dicts(obj: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            found.append(cur)
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return found


def _provenance_values(obj: Any) -> list[Any]:
    return [
        d["provenance"] for d in _walk_dicts(obj) if "provenance" in d
    ]


# -- harness ----------------------------------------------------------------


class _Harness:
    """One isolated environment per scenario: tmp store, config, clients."""

    def __init__(self, tmp_path: Path, config: dict[str, Any]) -> None:
        self.tmp_path = tmp_path
        self.store = SQLiteTraceStore(tmp_path / "golden.db")
        cfg: dict[str, Any] = {
            "data_dir": tmp_path,
            "db_path": tmp_path / "golden.db",
            **config,
        }
        if isinstance(cfg.get("capture"), str):
            cfg["capture"] = CaptureLevel(cfg["capture"])
        self.config = LensConfig(**cfg)
        self._client: Any = None
        self._pipeline: IngestPipeline | None = None

    def close(self) -> None:
        if self._client is not None:
            self._client.__exit__(None, None, None)
            self._client = None
        self.store.close()

    def client(self) -> Any:
        if self._client is None:
            from fastapi.testclient import TestClient

            from forge_lens.api.app import create_app

            self._client = TestClient(
                create_app(store=self.store, config=self.config)
            )
            self._client.__enter__()
        return self._client

    def pipeline(self) -> IngestPipeline:
        if self._pipeline is None:
            self._pipeline = IngestPipeline(self.store, self.config)
        return self._pipeline

    def apply_setup(self, setup: dict[str, Any]) -> None:
        for k in setup.get("api_keys", []):
            self.store.create_api_key(
                k["key_id"],
                name=k["name"],
                key_hash=hash_secret(k["secret"]),
                role=k["role"],
                projects=k.get("projects"),
            )
        for raw in setup.get("store_traces", []):
            self.store.upsert_trace(Trace.model_validate(raw))
        if setup.get("ingest_traces"):
            self.pipeline().ingest(
                [Trace.model_validate(t) for t in setup["ingest_traces"]]
            )
        for req in setup.get("requests", []):
            r = self._http(req)
            if r.status_code >= 300:
                raise ScenarioError(
                    f"setup request {req['method']} {req['path']} returned "
                    f"{r.status_code}: {r.text[:200]}"
                )

    def _http(self, req: dict[str, Any]) -> Any:
        kwargs: dict[str, Any] = {}
        for key in ("headers", "params", "json"):
            if key in req:
                kwargs[key] = req[key]
        if "content" in req:
            kwargs["content"] = req["content"].encode()
        return self.client().request(req["method"], req["path"], **kwargs)

    def execute(
        self,
        inp: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> dict[str, Any]:
        op = inp["op"]
        if op == "http":
            return {
                "kind": "http",
                "responses": [self._http(req) for req in inp["requests"]],
            }
        if op == "pipeline":
            traces = [Trace.model_validate(t) for t in inp["traces"]]
            calls: list[dict[str, Any]] = []
            for _ in range(inp.get("times", 1)):
                try:
                    res = self.pipeline().ingest(
                        traces, payload_bytes=inp.get("payload_bytes")
                    )
                    calls.append({"result": res, "error": None})
                except Exception as exc:  # the scenario decides how to assert
                    calls.append({"result": None, "error": exc})
            return {"kind": "pipeline", "calls": calls}
        if op == "engine":
            trace = self.store.get_trace(
                inp["trace_id"], project_id=inp.get("project_id")
            )
            if trace is None:
                raise ScenarioError(
                    f"engine input: trace {inp['trace_id']!r} not stored — "
                    "seed it via setup.store_traces or setup.ingest_traces"
                )
            out = _engine_output(inp["function"], trace, inp.get("params") or {})
            return {"kind": "engine", "output": out}
        if op == "mcp":
            return {"kind": "mcp", "output": self._mcp(inp, monkeypatch, capsys)}
        raise ScenarioError(f"unknown op {op!r}")

    def _mcp(
        self,
        inp: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> dict[str, Any]:
        from forge_lens.mcp_server import serve

        # serve() loads .forge-lens/config.toml from cwd + FORGE_LENS_*
        # env for the pricing registry — pin both for determinism.
        monkeypatch.chdir(self.tmp_path)
        monkeypatch.delenv("FORGE_LENS_PRICING_FILE", raising=False)
        stdin = io.StringIO(
            "".join(json.dumps(f) + "\n" for f in inp["frames"])
        )
        old = sys.stdin
        sys.stdin = stdin
        try:
            rc = serve(str(self.store.path))
        finally:
            sys.stdin = old
        replies = [
            json.loads(line)
            for line in capsys.readouterr().out.splitlines()
            if line.strip()
        ]
        out = {str(o.get("id")): o for o in replies}
        out["_rc"] = rc
        return out


def _engine_output(
    name: str, trace: Trace, params: dict[str, Any]
) -> dict[str, Any]:
    if name == "detect_loops":
        from forge_lens.ops.loops import detect_loops

        return detect_loops(trace, **params)
    if name == "classify_failure":
        from forge_lens.investigate import classify_failure

        return classify_failure(trace).model_dump(mode="json")
    if name == "replay_timeline":
        from forge_lens.investigate import replay_timeline

        return replay_timeline(trace)
    if name == "build_trace_graph":
        from forge_lens.graph import build_trace_graph

        return build_trace_graph(trace).model_dump(mode="json")
    if name == "inspect_trace":
        from forge_lens.security.mcp_inspect import inspect_trace

        return inspect_trace(trace)
    if name == "trace_efficiency":
        from forge_lens.intel.efficiency import trace_efficiency

        return trace_efficiency(trace)
    raise ScenarioError(f"unknown engine function {name!r}")


# -- assertion engine --------------------------------------------------------


def _result_dict(result: Any) -> dict[str, Any]:
    return {
        "accepted_spans": result.accepted_spans,
        "updated_spans": result.updated_spans,
        "duplicate_spans": result.duplicate_spans,
        "traces": result.traces,
        "sanitize": result.sanitize,
        "warnings": result.warnings,
    }


def _check_response(r_exp: dict[str, Any], resp: Any, where: str) -> list[str]:
    problems: list[str] = []
    if "status" in r_exp and resp.status_code != r_exp["status"]:
        problems.append(
            f"{where}.status: expected {r_exp['status']}, got {resp.status_code}"
        )
    body: Any = None
    if "json" in r_exp or "json_contains" in r_exp:
        try:
            body = resp.json()
        except json.JSONDecodeError:
            problems.append(f"{where}: response body is not JSON")
            return problems
    if "json" in r_exp:
        problems.extend(_subset(r_exp["json"], body, f"{where}.json"))
    if "json_contains" in r_exp:
        problems.extend(
            _assert_contains(r_exp["json_contains"], body, f"{where}.json_contains")
        )
    for s in r_exp.get("body_contains", []):
        if s not in resp.text:
            problems.append(f"{where}.body_contains: {s!r} absent")
    return problems


def _span_lookup(
    store: SQLiteTraceStore, key: str, where: str
) -> tuple[dict[str, Any] | None, str | None]:
    """``<trace_id>/<span_id>`` → stored span dump (or an error string)."""
    if "/" not in key:
        return None, f"{where}: span key {key!r} must be <trace_id>/<span_id>"
    tid, sid = key.split("/", 1)
    trace = store.get_trace(tid)
    if trace is None:
        return None, f"{where}: trace {tid!r} not stored"
    span = next((s for s in trace.spans if s.span_id == sid), None)
    if span is None:
        return None, f"{where}: span {sid!r} not in trace {tid!r}"
    return span.model_dump(mode="json"), None


def _row_matches(
    expected_rows: list[dict[str, Any]],
    actual_rows: list[dict[str, Any]],
    where: str,
) -> list[str]:
    """Every expected row-subset must match at least one actual row."""
    problems: list[str] = []
    for i, e in enumerate(expected_rows):
        if not any(not _subset(e, row, where) for row in actual_rows):
            problems.append(f"{where}[{i}]: no stored row matches {e!r}")
    return problems


def _assert_store(
    store: SQLiteTraceStore, exp: dict[str, Any]
) -> list[str]:
    problems: list[str] = []
    rows = store.list_traces()
    if "trace_count" in exp and len(rows) != exp["trace_count"]:
        problems.append(
            f"store.trace_count: expected {exp['trace_count']}, got {len(rows)}"
        )
    if "span_count" in exp:
        actual = int(store.stats()["spans"])
        if actual != exp["span_count"]:
            problems.append(
                f"store.span_count: expected {exp['span_count']}, got {actual}"
            )
    for tid, fields in exp.get("traces", {}).items():
        trace = store.get_trace(tid)
        if trace is None:
            problems.append(f"store.traces.{tid}: trace not stored")
            continue
        problems.extend(
            _subset(fields, trace.model_dump(mode="json"), f"store.traces.{tid}")
        )
    for key, fields in exp.get("spans", {}).items():
        span, err = _span_lookup(store, key, "store.spans")
        if err:
            problems.append(err)
        else:
            problems.extend(_subset(fields, span, f"store.spans.{key}"))
    for key, flags in exp.get("span_flags", {}).items():
        span, err = _span_lookup(store, key, "store.span_flags")
        if err:
            problems.append(err)
            continue
        assert span is not None
        have = set(span["integrity_flags"])
        for f in flags:
            if f not in have:
                problems.append(
                    f"store.span_flags.{key}: flag {f!r} absent (have {sorted(have)})"
                )
    for key, flags in exp.get("span_flag_absent", {}).items():
        span, err = _span_lookup(store, key, "store.span_flag_absent")
        if err:
            problems.append(err)
            continue
        assert span is not None
        have = set(span["integrity_flags"])
        for f in flags:
            if f in have:
                problems.append(
                    f"store.span_flag_absent.{key}: flag {f!r} unexpectedly present"
                )
    for key, attrs in exp.get("span_attrs", {}).items():
        span, err = _span_lookup(store, key, "store.span_attrs")
        if err:
            problems.append(err)
        else:
            problems.extend(
                _subset(attrs, span["attributes"], f"store.span_attrs.{key}")
            )
    if "sessions" in exp:
        problems.extend(
            _row_matches(exp["sessions"], store.list_sessions(), "store.sessions")
        )
    if "tools" in exp:
        problems.extend(
            _row_matches(exp["tools"], store.list_tools(), "store.tools")
        )
    if "audit" in exp:
        problems.extend(
            _row_matches(exp["audit"], store.list_audit(limit=500), "store.audit")
        )
    return problems


def _assert_forbidden(
    forbidden: dict[str, Any],
    op_result: dict[str, Any],
    store: SQLiteTraceStore,
) -> list[str]:
    problems: list[str] = []
    bodies: list[str] = []
    output_json = ""
    if op_result["kind"] == "http":
        bodies = [r.text for r in op_result["responses"]]
    else:
        payload = (
            op_result.get("output")
            or [
                _result_dict(c["result"]) if c["result"] else repr(c["error"])
                for c in op_result.get("calls", [])
            ]
        )
        output_json = json.dumps(payload, sort_keys=True, default=str)
        bodies = [output_json]
    for s in forbidden.get("body_not_contains", []):
        for i, text in enumerate(bodies):
            if s in text:
                problems.append(f"forbidden.body_not_contains: {s!r} in response {i}")
    for s in forbidden.get("output_not_contains", []):
        if s in output_json:
            problems.append(f"forbidden.output_not_contains: {s!r} in output")
    if forbidden.get("store_json_not_contains") or forbidden.get("trace_ids_absent"):
        stored: list[str] = []
        for row in store.list_traces():
            t = store.get_trace(row["trace_id"], project_id=row["project_id"])
            if t is not None:
                stored.append(json.dumps(t.model_dump(mode="json"), default=str))
        for s in forbidden.get("store_json_not_contains", []):
            if any(s in blob for blob in stored):
                problems.append(
                    f"forbidden.store_json_not_contains: {s!r} in stored trace"
                )
        for tid in forbidden.get("trace_ids_absent", []):
            if any(r["trace_id"] == tid for r in store.list_traces()):
                problems.append(
                    f"forbidden.trace_ids_absent: {tid!r} still stored"
                )
            for text in bodies:
                if tid in text:
                    problems.append(
                        f"forbidden.trace_ids_absent: {tid!r} in response body"
                    )
    audit_blob = json.dumps(store.list_audit(limit=500), default=str)
    for s in forbidden.get("audit_not_contains", []):
        if s in audit_blob:
            problems.append(f"forbidden.audit_not_contains: {s!r} in audit trail")
    if forbidden.get("status_not") and op_result["kind"] == "http":
        banned = set(forbidden["status_not"])
        for i, r in enumerate(op_result["responses"]):
            if r.status_code in banned:
                problems.append(
                    f"forbidden.status_not: response {i} has {r.status_code}"
                )
    return problems


def _op_output_json(op_result: dict[str, Any]) -> Any:
    """The JSON shape used by evidence/provenance scans."""
    if op_result["kind"] == "http":
        last = op_result["responses"][-1]
        try:
            return last.json()
        except json.JSONDecodeError:
            return {"_text": last.text}
    if op_result["kind"] == "pipeline":
        return [
            _result_dict(c["result"]) if c["result"] else {"error": repr(c["error"])}
            for c in op_result["calls"]
        ]
    return op_result.get("output")


def _assert_evidence(op_result: dict[str, Any]) -> list[str]:
    """Every evidence-bearing object must carry non-empty evidence."""
    payload = _op_output_json(op_result)
    holders = [
        d for d in _walk_dicts(payload) if any(k in d for k in _EVIDENCE_KEYS)
    ]
    if not holders:
        return [
            "evidence_required: no evidence-bearing object "
            f"({', '.join(_EVIDENCE_KEYS)}) found in op output"
        ]
    problems = []
    for d in holders:
        for k in _EVIDENCE_KEYS:
            if k in d and not d[k]:
                problems.append(f"evidence_required: {k!r} is empty in {d!r:.120}")
    return problems


def _assert_provenance(declared: str, op_result: dict[str, Any]) -> list[str]:
    values = _provenance_values(_op_output_json(op_result))
    if not values:
        return [
            f"provenance: declared {declared!r} but op output carries no "
            "provenance field"
        ]
    bad = [v for v in values if v != declared]
    if bad:
        return [f"provenance: expected {declared!r}, observed {bad!r}"]
    return []


def _run_scenario(
    scenario: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> list[str]:
    harness = _Harness(tmp_path, scenario.get("setup", {}).get("config", {}))
    problems: list[str] = []
    try:
        harness.apply_setup(scenario.get("setup", {}))
        op_result = harness.execute(scenario["input"], monkeypatch, capsys)
        exp = scenario["expected"]

        if op_result["kind"] == "http":
            responses = op_result["responses"]
            if "status" in exp:
                for i, r in enumerate(responses):
                    if r.status_code != exp["status"]:
                        problems.append(
                            f"expected.status: response {i} got {r.status_code}, "
                            f"expected {exp['status']} — {r.text[:200]}"
                        )
            if "statuses" in exp:
                if len(exp["statuses"]) != len(responses):
                    problems.append(
                        f"expected.statuses: {len(exp['statuses'])} codes for "
                        f"{len(responses)} responses"
                    )
                else:
                    for i, (want, r) in enumerate(
                        zip(exp["statuses"], responses, strict=True)
                    ):
                        if r.status_code != want:
                            problems.append(
                                f"expected.statuses[{i}]: got {r.status_code}, "
                                f"expected {want} — {r.text[:200]}"
                            )
            for i, r_exp in enumerate(exp.get("responses", [])):
                if i >= len(responses):
                    problems.append(f"expected.responses[{i}]: no such response")
                    continue
                problems.extend(
                    _check_response(r_exp, responses[i], f"expected.responses[{i}]")
                )
            last_check = {
                k: exp[k]
                for k in ("json", "json_contains", "body_contains")
                if k in exp
            }
            if last_check:
                problems.extend(_check_response(last_check, responses[-1], "expected"))
        elif op_result["kind"] == "pipeline":
            calls = op_result["calls"]
            if "results" in exp:
                if len(exp["results"]) != len(calls):
                    problems.append(
                        f"expected.results: {len(exp['results'])} entries for "
                        f"{len(calls)} calls"
                    )
                else:
                    for i, (e, c) in enumerate(
                        zip(exp["results"], calls, strict=True)
                    ):
                        if c["result"] is None:
                            problems.append(
                                f"expected.results[{i}]: call raised "
                                f"{c['error']!r}"
                            )
                            continue
                        problems.extend(
                            _subset(e, _result_dict(c["result"]), f"results[{i}]")
                        )
            if "result" in exp:
                last = calls[-1]
                if last["result"] is None:
                    problems.append(
                        f"expected.result: call raised {last['error']!r}"
                    )
                else:
                    problems.extend(
                        _subset(exp["result"], _result_dict(last["result"]), "result")
                    )
            if "error" in exp:
                last = calls[-1]
                if last["error"] is None:
                    problems.append(
                        "expected.error: no exception raised by last call"
                    )
                else:
                    exc = last["error"]
                    want = exp["error"]
                    if "type" in want and type(exc).__name__ != want["type"]:
                        problems.append(
                            f"expected.error.type: {type(exc).__name__} "
                            f"!= {want['type']}"
                        )
                    if "contains" in want and want["contains"] not in str(exc):
                        problems.append(
                            f"expected.error.contains: {want['contains']!r} "
                            f"absent in {exc}"
                        )
        else:
            out = op_result["output"]
            if "output" in exp:
                problems.extend(_subset(exp["output"], out, "expected.output"))
            if "output_contains" in exp:
                problems.extend(
                    _assert_contains(
                        exp["output_contains"], out, "expected.output_contains"
                    )
                )

        if "store" in exp:
            problems.extend(_assert_store(harness.store, exp["store"]))
        problems.extend(
            _assert_forbidden(scenario["forbidden"], op_result, harness.store)
        )
        if scenario.get("evidence_required"):
            problems.extend(_assert_evidence(op_result))
        if "provenance" in scenario:
            problems.extend(_assert_provenance(scenario["provenance"], op_result))
    finally:
        harness.close()
    return problems


# -- the corpus --------------------------------------------------------------


def _scenario_files() -> list[Path]:
    return sorted(SCENARIOS_DIR.glob("*.json"))


def test_corpus_is_present_and_unique() -> None:
    files = _scenario_files()
    assert len(files) >= MIN_SCENARIOS, (
        f"golden corpus shrank to {len(files)} scenarios (< {MIN_SCENARIOS}) — "
        "a legitimate reduction is a review decision and must lower "
        "MIN_SCENARIOS deliberately"
    )
    ids = [f.stem for f in files]
    assert len(ids) == len(set(ids)), "duplicate scenario_id filenames"


def test_corpus_schema_valid() -> None:
    """Every scenario file must parse + validate — malformed corpus fails."""
    problems: list[str] = []
    for path in _scenario_files():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path.name}: invalid JSON — {exc}")
            continue
        try:
            _validate(path, data)
        except ScenarioError as exc:
            problems.append(str(exc))
    assert not problems, "malformed golden scenario(s):\n" + "\n".join(problems)


@pytest.mark.parametrize(
    "path", _scenario_files(), ids=lambda p: p.stem
)
def test_golden_scenario(
    path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    scenario = _validate(path, data)
    problems = _run_scenario(scenario, tmp_path, monkeypatch, capsys)
    assert not problems, (
        f"scenario {scenario['scenario_id']} failed:\n"
        + "\n".join(f"  - {p}" for p in problems)
    )
