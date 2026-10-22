"""MCP stdio observer server — Cycle 10, expanded in Cycle 13.

A minimal JSON-RPC 2.0 MCP server over stdin/stdout exposing read-only
Lens tools. Real MCP surface: `tools/list`, `tools/call`,
`initialize`, `ping` — enough for a client to observe stored traces
through the protocol it already speaks. The tool set mirrors the API's
core read intelligence (loops, efficiency, cost, failure, datasets,
capabilities) so MCP-capable agents can investigate traces without a
running HTTP server.

Wire contract (docs/reference/mcp.md): every request is gated on
`initialize` except `ping`; frames without `id` are notifications and
never answered. Errors: -32700 parse, -32600 invalid request, -32601
unknown method, -32602 invalid params/pre-init, -32603 opaque internal
faults; data misses ride back as `result.isError`, never as protocol
faults — one bad frame or call never kills the loop.
"""

from __future__ import annotations

import io
import json
import math
import sys
from typing import Any, NoReturn

from forge_lens import __version__
from forge_lens.contracts.v1.trace import Trace
from forge_lens.core.jsonutil import MAX_JSON_DEPTH as _MAX_JSON_DEPTH
from forge_lens.core.jsonutil import json_depth as _json_depth
from forge_lens.storage.sqlite import SQLiteTraceStore

# The one MCP protocol revision this server speaks. Negotiation follows
# the spec: a requested version we support is echoed back, otherwise the
# answer carries ours and the client decides whether it can proceed.
_PROTOCOL_VERSION = "2024-11-05"
_SUPPORTED_PROTOCOLS = frozenset({_PROTOCOL_VERSION})
# Per-frame size bound on the stdio wire — same 8 MiB posture the API
# applies to request bodies. Text-mode reads count chars, so the byte
# cost is at most ~4x this on a non-ASCII frame — bounded either way.
_MAX_LINE_CHARS = 8 * 1024 * 1024


def _reject_constant(value: str) -> NoReturn:
    """``json.loads`` hook — JSON-RPC runs on strict JSON, so the
    ``NaN``/``Infinity`` literals Python would otherwise accept are a
    parse error (-32700) on the wire."""
    raise ValueError(f"non-JSON literal {value}")


def _num(args: dict[str, Any], name: str,
         *, integer: bool = False) -> int | float | None:
    """Optional numeric arg — bools, non-numerics and non-finite floats
    raise the authored TypeError that maps to -32602 invalid params.

    A string form is normalized here: ``int`` first (the shape every
    consumer wants), ``float`` as the fractional fallback. Anything
    unparseable is invalid-params (-32602) — letting it ride through
    would make ``int()`` raise ValueError inside the call and leak an
    -32603 internal error for what is plainly a client mistake.

    ``integer=True`` (limit/offset-style params) requires an integral
    value — a fractional ``2.5`` is a client error (-32602), not a
    silent ``int()`` truncation that would answer a different query
    than the one asked. Integral floats/strings still normalize."""
    v = args.get(name)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        raise TypeError(f"{name} must be a number")
    if isinstance(v, float) and not math.isfinite(v):
        raise TypeError(f"{name} must be a number")
    if isinstance(v, str):
        if "_" in v:
            # int()/float() accept Python-literal underscores ("5_0"→50)
            # — not valid JSON-number syntax, so a wire string carrying
            # one is invalid params, not a silent normalization.
            raise TypeError(f"{name} must be a number")
        try:
            return int(v)
        except ValueError:
            pass
        try:
            f = float(v)
        except ValueError:
            raise TypeError(f"{name} must be a number") from None
        if not math.isfinite(f):
            raise TypeError(f"{name} must be a number")
        v = f
    if integer and isinstance(v, float):
        if not v.is_integer():
            raise TypeError(f"{name} must be an integer")
        return int(v)
    return v

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
             "offset": {"type": "integer", "default": 0},
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
             "offset": {"type": "integer", "default": 0},
             "project_id": {"type": "string"}}}},
        {"name": "lens.trace_efficiency",
         "description": "Per-trace efficiency roll-up "
                        "(tokens/tools/retries).",
         "inputSchema": _TRACE_INPUT},
        {"name": "lens.correlate",
         "description": "Temporal co-occurrence signals within a trace "
                        "(retries→errors, model switches, context growth). "
                        "Descriptive associations, never causal claims.",
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
    # MCP stdio is UTF-8 on the wire. Without this, text-mode stdin
    # decodes with the platform locale (cp1252 on Windows): conforming
    # UTF-8 frames arrive mojibake'd or a UnicodeDecodeError inside
    # readline() kills the loop. errors="replace" makes undecodable
    # input degrade to a -32700 parse error instead of a crash.
    # StringIO (tests) isn't a TextIOWrapper — skipped by the check.
    if isinstance(sys.stdin, io.TextIOWrapper):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
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
            raw_lim = _num(args, "limit", integer=True)
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
        if name == "lens.correlate":
            from forge_lens.intel.correlation import correlate_trace

            tid, trace = _trace_lookup(args)
            if trace is None:
                return _missing(tid)
            return correlate_trace(trace)
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
        def _page_args(default_limit: int) -> tuple[int, int]:
            """Bounded limit/offset shared by the roll-up tools."""
            lim_raw = _num(args, "limit", integer=True)
            off_raw = _num(args, "offset", integer=True)
            lim = default_limit if lim_raw is None else int(lim_raw)
            off = 0 if off_raw is None else int(off_raw)
            if off > 2**63 - 1:
                # out of sqlite int64 — same 422-class input error the
                # API rejects; authored TypeError → -32602 on the wire
                raise TypeError("offset out of range")
            return min(500, max(0, lim)), max(0, off)

        if name == "lens.sessions":
            lim, off = _page_args(100)
            return {"sessions": store.list_sessions(
                _pid(args), limit=lim, offset=off)}
        if name == "lens.tools":
            lim, off = _page_args(100)
            return {"tools": store.list_tools(
                _pid(args), limit=lim, offset=off)}
        if name == "lens.capabilities":
            from forge_lens.adapters.capabilities import capability_matrix

            return capability_matrix()
        return {"error": f"unknown tool {name!r}"}

    def _err(rid: Any, code: int, message: str) -> None:
        out = {"jsonrpc": "2.0", "id": rid,
               "error": {"code": code, "message": message}}
        print(json.dumps(out), flush=True)

    initialized = False
    while True:
        # Bounded readline — a multi-GB single-line frame is the same
        # hostile-payload class the API rejects at max_payload_bytes;
        # text-mode (tests feed StringIO) so the bound counts chars.
        line = sys.stdin.readline(_MAX_LINE_CHARS + 2)
        if not line:
            break
        # Content of C chars + '\n' reads C+1: C == MAX is legal
        # (len MAX+1), C > MAX always lands past that (or misses the
        # newline inside the read window).
        if len(line) > _MAX_LINE_CHARS + 1:
            # Over-size frame — drain to the newline so the remainder
            # doesn't spawn a cascade of bogus frames, then parse-error.
            while not line.endswith("\n"):
                line = sys.stdin.readline(_MAX_LINE_CHARS + 2)
                if not line:
                    break
            _err(None, -32700, "parse error")
            continue
        line = line.strip()
        if not line:
            continue
        try:
            if _json_depth(line) > _MAX_JSON_DEPTH:
                # Reject hostile nesting lexically — json.loads recursion
                # tolerance differs per CPython build, so the bound lives
                # here where it is identical on every platform.
                raise ValueError("JSON nesting too deep")
            req = json.loads(line, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            # Parse error — malformed JSON, over-deep nesting and
            # stack-overflow all land here; the id is unknowable,
            # JSON-RPC says null.
            _err(None, -32700, "parse error")
            continue
        if not isinstance(req, dict):
            # Non-object frame (batch/primitive) — invalid request.
            _err(None, -32600, "invalid request")
            continue
        method = req.get("method")
        rid = req.get("id")
        if rid is None:
            continue  # notifications need no reply
        if method == "notifications/initialized":
            # A frame carrying an id is a Request object — it can't be a
            # notification, so the spec-true reply is invalid-request.
            _err(rid, -32600, "invalid request")
            continue
        if not isinstance(method, str):
            # An id without a dispatchable method is an invalid request.
            _err(rid, -32600, "invalid request")
            continue
        # MCP lifecycle: pings are allowed during the handshake —
        # everything else waits for initialize.
        if not initialized and method not in ("initialize", "ping"):
            _err(rid, -32602, "expected 'initialize' first")
            continue
        result: dict[str, Any]
        if method == "initialize":
            if initialized:
                # initialize is a once-per-connection handshake — a
                # repeat is an invalid request (-32600), not a
                # renegotiation. The spec's lifecycle binds it to the
                # pre-initialized window only.
                _err(rid, -32600, "already initialized")
                continue
            params = req.get("params")
            requested = (
                params.get("protocolVersion")
                if isinstance(params, dict) else None
            )
            result = {
                "protocolVersion": (
                    requested
                    # A non-string version can't be supported — and an
                    # unhashable one (object/array) would TypeError the
                    # `in` probe and kill the loop.
                    if isinstance(requested, str)
                    and requested in _SUPPORTED_PROTOCOLS
                    else _PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "forge-lens",
                               "version": __version__},
            }
            # Mark ready only after the negotiation result exists — an
            # exception mid-handshake must not leave the loop half-open.
            initialized = True
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": _tools()}
        elif method == "tools/call":
            p = req.get("params")
            if not isinstance(p, dict):
                _err(rid, -32602, "params must be an object")
                continue
            tname = p.get("name")
            if not isinstance(tname, str):
                # `name` is a required CallToolParams field — absent or
                # mistyped is invalid params, never an unknown-tool miss.
                _err(rid, -32602, "name must be a string")
                continue
            args = p.get("arguments")
            if args is None:
                args = {}
            elif not isinstance(args, dict):
                _err(rid, -32602, "arguments must be an object")
                continue
            try:
                payload = _call(tname, args)
                content: list[dict[str, str]] = [{
                    "type": "text", "text": json.dumps(payload)}]
                # Tool-level failure is a successful call carrying
                # isError content — contract: any payload with a
                # top-level "error" key is flagged isError here.
                result = {"content": content}
                if isinstance(payload, dict) and "error" in payload:
                    result["isError"] = True
            except TypeError:
                # authored arg-validation raises (bad/mistyped fields)
                # are invalid-params, not internal faults; an impl bug
                # raising TypeError lands here too — the message stays
                # static either way since arg detail can embed payloads
                _err(rid, -32602, "invalid params")
                continue
            except Exception as exc:  # observer loop: a bad call never kills it
                # one malformed call must not kill the stdio loop; the
                # wire message is opaque — exception text can embed
                # payload fragments or paths, so only the type name
                # crosses the boundary.
                _err(rid, -32603,
                     f"internal tool error: {type(exc).__name__}")
                continue
        else:
            _err(rid, -32601, f"unknown {method!r}")
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}),
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(serve(sys.argv[1] if len(sys.argv) > 1 else "./lens.db"))
