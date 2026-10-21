"""MCP stdio observer server — Cycle 10.

A minimal JSON-RPC 2.0 MCP server over stdin/stdout exposing read-only
Lens tools. Real MCP surface: `tools/list`, `tools/call`,
`initialize`, `ping` — enough for a client to observe stored traces
through the protocol it already speaks.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from forge_lens import __version__
from forge_lens.storage.sqlite import SQLiteTraceStore

_METHODS = ("initialize", "ping", "tools/list", "tools/call", "notifications/initialized")


def _tools() -> list[dict[str, Any]]:
    return [
        {"name": "lens.list_traces",
         "description": "List recent traces (bounded, sorted).",
         "inputSchema": {"type": "object", "properties": {
             "limit": {"type": "integer", "default": 20},
             "project_id": {"type": "string"}}}},
        {"name": "lens.get_trace",
         "description": "Fetch one trace by id.",
         "inputSchema": {"type": "object", "required": ["trace_id"],
                         "properties": {"trace_id": {"type": "string"},
                                        "project_id": {"type": "string"}}}},
        {"name": "lens.stats",
         "description": "Store statistics snapshot.",
         "inputSchema": {"type": "object", "properties": {}}},
    ]


def serve(db_path: str) -> int:
    """Blocking stdio loop — one JSON-RPC message per line."""
    store = SQLiteTraceStore(db_path, readonly=True)

    def _call(name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "lens.list_traces":
            pid = args.get("project_id")
            if pid is not None and not isinstance(pid, str):
                raise TypeError("project_id must be a string")
            return {"traces": store.list_traces(
                project_id=pid,
                limit=min(500, int(args.get("limit") or 20)))}
        if name == "lens.get_trace":
            tid = args.get("trace_id")
            if not isinstance(tid, str) or not tid:
                raise TypeError("trace_id must be a non-empty string")
            pid = args.get("project_id")
            if pid is not None and not isinstance(pid, str):
                raise TypeError("project_id must be a string")
            t = store.get_trace(tid, project_id=pid)
            return {"trace": t.model_dump(mode="json") if t else None}
        if name == "lens.stats":
            return store.stats()
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
