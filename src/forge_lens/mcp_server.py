"""MCP stdio observer server — Cycle 10, expanded in Cycle 13.

A minimal JSON-RPC 2.0 MCP server over stdin/stdout exposing read-only
Lens tools. Real MCP surface: `tools/list`, `tools/call`,
`initialize`, `ping` — enough for a client to observe stored traces
through the protocol it already speaks. The tool set mirrors the API's
core read intelligence (loops, efficiency, cost, failure, datasets,
capabilities) so MCP-capable agents can investigate traces without a
running HTTP server.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from forge_lens import __version__
from forge_lens.contracts.v1.trace import Trace
from forge_lens.storage.sqlite import SQLiteTraceStore

_METHODS = ("initialize", "ping", "tools/list", "tools/call", "notifications/initialized")

_TRACE_INPUT = {
    "type": "object",
    "required": ["trace_id"],
    "properties": {
        "trace_id": {"type": "string"},
        "project_id": {"type": "string"},
    },
}


def _tools() -> list[dict[str, Any]]:
    return [
        {"name": "lens.capabilities",
         "description": "Host adapter capability matrix — honest "
                        "coverage claims.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "lens.cost",
         "description": "Derived cost report for one trace "
                        "(registry-priced; includes waste findings).",
         "inputSchema": _TRACE_INPUT},
        {"name": "lens.detect_loops",
         "description": "Heuristic loop/no-progress findings with "
                        "span evidence.",
         "inputSchema": _TRACE_INPUT},
        {"name": "lens.failure",
         "description": "Deterministic failure classification with "
                        "span evidence.",
         "inputSchema": _TRACE_INPUT},
        {"name": "lens.get_trace",
         "description": "Fetch one trace by id.",
         "inputSchema": _TRACE_INPUT},
        {"name": "lens.list_datasets",
         "description": "List eval datasets in the store.",
         "inputSchema": {"type": "object", "properties": {
             "project_id": {"type": "string"}}}},
        {"name": "lens.list_traces",
         "description": "List recent traces (bounded, sorted).",
         "inputSchema": {"type": "object", "properties": {
             "limit": {"type": "integer", "default": 20},
             "project_id": {"type": "string"}}}},
        {"name": "lens.sessions",
         "description": "Session roll-up — one row per session_id, "
                        "newest activity first (matches /v1/sessions).",
         "inputSchema": {"type": "object", "properties": {
             "limit": {"type": "integer", "default": 100},
             "project_id": {"type": "string"}}}},
        {"name": "lens.stats",
         "description": "Store statistics snapshot.",
         "inputSchema": {"type": "object", "properties": {
             "project_id": {"type": "string"}}}},
        {"name": "lens.tools",
         "description": "Fleet-wide tool roll-up — calls/traces/errors/"
                        "avg duration per tool.name (matches /v1/tools).",
         "inputSchema": {"type": "object", "properties": {
             "limit": {"type": "integer", "default": 100},
             "project_id": {"type": "string"}}}},
        {"name": "lens.trace_efficiency",
         "description": "Per-trace efficiency roll-up "
                        "(tokens/tools/retries).",
         "inputSchema": _TRACE_INPUT},
    ]


def _pid(args: dict[str, Any]) -> str | None:
    pid = args.get("project_id")
    if pid is not None and not isinstance(pid, str):
        raise TypeError("project_id must be a string")
    return pid


def _tid_pid(args: dict[str, Any]) -> tuple[str, str | None]:
    tid = args.get("trace_id")
    if not isinstance(tid, str) or not tid:
        raise TypeError("trace_id must be a non-empty string")
    return tid, _pid(args)


def _missing(trace_id: str) -> dict[str, Any]:
    """Not-found is data, not a protocol fault — the loop keeps serving."""
    return {"trace_id": trace_id, "error": "trace not found"}


def serve(db_path: str) -> int:
    """Blocking stdio loop — one JSON-RPC message per line."""
    try:
        store = SQLiteTraceStore(db_path, readonly=True)
    except Exception as exc:
        print(f"cannot open store at {db_path}: {exc}", file=sys.stderr)
        return 2

    # Identical construction to api/app.py: the registry comes from
    # LensConfig.pricing_file (FORGE_LENS_PRICING_FILE / config.toml) or
    # the bundled default. A malformed pricing file is an operator-config
    # error — it fails fast here, once, never as a per-call fault.
    from forge_lens.core.config import load_config
    from forge_lens.intel.pricing import PricingRegistry

    try:
        pricing_file = load_config().pricing_file
        pricing = (
            PricingRegistry.from_file(pricing_file)
            if pricing_file
            else PricingRegistry()
        )
    except Exception as exc:
        print(f"cannot load pricing registry: {exc}", file=sys.stderr)
        return 2

    def _trace_lookup(args: dict[str, Any]) -> tuple[str, Trace | None]:
        tid, pid = _tid_pid(args)
        return tid, store.get_trace(tid, project_id=pid)

    def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "lens.list_traces":
            pid = _pid(args)
            raw_lim = args.get("limit")
            if raw_lim is not None and (
                isinstance(raw_lim, bool)
                or not isinstance(raw_lim, (int, float, str))
            ):
                raise TypeError("limit must be a number")
            limit = 20 if raw_lim is None else int(raw_lim)
            return {"traces": store.list_traces(
                project_id=pid,
                limit=min(500, max(0, limit)))}
        if name == "lens.get_trace":
            tid, pid = _tid_pid(args)
            t = store.get_trace(tid, project_id=pid)
            return {"trace": t.model_dump(mode="json") if t else None}
        if name == "lens.stats":
            return store.stats(project_id=_pid(args))
        if name == "lens.detect_loops":
            from forge_lens.ops.loops import detect_loops

            tid, trace = _trace_lookup(args)
            if trace is None:
                return _missing(tid)
            return detect_loops(trace)
        if name == "lens.trace_efficiency":
            from forge_lens.intel.efficiency import trace_efficiency

            tid, trace = _trace_lookup(args)
            if trace is None:
                return _missing(tid)
            return trace_efficiency(trace)
        if name == "lens.cost":
            from forge_lens.intel import detect_waste, trace_cost

            tid, trace = _trace_lookup(args)
            if trace is None:
                return _missing(tid)
            # same shape as GET /v1/traces/{id}/cost
            out = trace_cost(trace, pricing).model_dump(mode="json")
            out["pricing_source"] = pricing.source
            out["waste"] = [
                w.model_dump(mode="json")
                for w in detect_waste(trace, pricing)
            ]
            return out
        if name == "lens.failure":
            from forge_lens.investigate import classify_failure

            tid, trace = _trace_lookup(args)
            if trace is None:
                return _missing(tid)
            return classify_failure(trace).model_dump(mode="json")
        if name == "lens.list_datasets":
            return {"datasets": store.list_datasets(_pid(args))}
        if name == "lens.sessions":
            raw_lim = args.get("limit")
            if raw_lim is not None and (
                isinstance(raw_lim, bool)
                or not isinstance(raw_lim, (int, float, str))
            ):
                raise TypeError("limit must be a number")
            lim = 100 if raw_lim is None else int(raw_lim)
            return {"sessions": store.list_sessions(
                _pid(args), limit=min(500, max(0, lim)))}
        if name == "lens.tools":
            raw_lim = args.get("limit")
            if raw_lim is not None and (
                isinstance(raw_lim, bool)
                or not isinstance(raw_lim, (int, float, str))
            ):
                raise TypeError("limit must be a number")
            lim = 100 if raw_lim is None else int(raw_lim)
            return {"tools": store.list_tools(
                _pid(args), limit=min(500, max(0, lim)))}
        if name == "lens.capabilities":
            from forge_lens.adapters.capabilities import capability_matrix

            return capability_matrix()
        return {"error": f"unknown tool {name!r}"}

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(req, dict):
            continue  # non-object frame — ignore, keep serving
        method = req.get("method")
        rid = req.get("id")
        if method in ("notifications/initialized",) or rid is None:
            continue  # notifications need no reply
        result: dict[str, Any]
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05",
                      "capabilities": {"tools": {}},
                      "serverInfo": {"name": "forge-lens", "version": __version__}}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            p = req.get("params")
            if not isinstance(p, dict):
                out = {"jsonrpc": "2.0", "id": rid, "error": {
                    "code": -32602, "message": "params must be an object"}}
                print(json.dumps(out), flush=True)
                continue
            try:
                args = p.get("arguments") or {}
                if not isinstance(args, dict):
                    raise TypeError("arguments must be an object")
                payload = _call(str(p.get("name")), args)
                content: list[dict[str, str]] = [{
                    "type": "text", "text": json.dumps(payload)}]
                result = {"content": content}
            except Exception as exc:  # observer loop: a bad call never kills it
                # one malformed call must not kill the stdio loop
                out = {"jsonrpc": "2.0", "id": rid,
                       "error": {"code": -32603,
                                 "message": f"tool error: {exc}"}}
                print(json.dumps(out), flush=True)
                continue
        else:
            out = {"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"unknown {method!r}"}}
            print(json.dumps(out), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}),
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve(sys.argv[1] if len(sys.argv) > 1 else "./lens.db"))
