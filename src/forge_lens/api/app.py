"""FastAPI application — OTLP endpoint, native ingest, query endpoints.

The API is a thin boundary: all real work lives in the pipeline/store.
The engine itself has no network dependency; this module is the only
place where HTTP is handled.
"""

from __future__ import annotations

import contextlib
import json
import math
import secrets
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import ValidationError as PydanticValidationError
from starlette.concurrency import run_in_threadpool

from forge_lens import __version__
from forge_lens.api.clock import wall_clock_ns
from forge_lens.contracts.v1.trace import TraceIngest
from forge_lens.core.config import LensConfig
from forge_lens.core.errors import (
    PayloadTooLargeError,
    RateLimitedError,
    ValidationError,
)
from forge_lens.core.metrics import (
    M_API_ERRORS,
    M_API_REQUESTS,
    M_INGEST_ERRORS,
    M_QUERY_LATENCY,
    METRICS,
)
from forge_lens.core.time import ensure_aware, to_unix_nano
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.otlp.normalize import normalize_otlp_request
from forge_lens.storage.base import TraceStore
from forge_lens.storage.sqlite import SQLiteTraceStore

API_VERSION = "v1"


def _reject_ingest(status_code: int, detail: str) -> HTTPException:
    """Every ingest rejection counts on both API and ingest error metrics."""
    METRICS.inc(M_API_ERRORS)
    METRICS.inc(M_INGEST_ERRORS)
    return HTTPException(status_code=status_code, detail=detail)


def _reject_request(status_code: int, detail: str) -> HTTPException:
    """Non-ingest rejection — API error metric only."""
    METRICS.inc(M_API_ERRORS)
    return HTTPException(status_code=status_code, detail=detail)


def _check_declared_size(
    request: Request,
    max_bytes: int,
    reject: Callable[[int, str], HTTPException] = _reject_ingest,
) -> None:
    """Fast-path 413 on Content-Length before reading any body bytes.

    A courtesy check only — the header can be absent (chunked) or lie;
    the real bound is ``_read_capped_body``.
    """
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) > max_bytes:
        raise reject(413, "payload too large")


async def _read_capped_body(
    request: Request,
    max_bytes: int,
    reject: Callable[[int, str], HTTPException] = _reject_ingest,
) -> bytes:
    """Stream the body and abort the moment it exceeds ``max_bytes``.

    ``request.body()``/``request.json()`` buffer the entire payload
    before any size check can run — a chunked or headerless POST of
    arbitrary size would be materialized first. Streaming keeps memory
    at ≤ max_bytes + one chunk. ``reject`` picks the metric family:
    ingest endpoints count ingest errors; query-by-body endpoints don't.
    """
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise reject(413, "payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _ingest_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PayloadTooLargeError):
        return _reject_ingest(413, str(exc))
    if isinstance(exc, RateLimitedError):
        return _reject_ingest(429, str(exc))
    return _reject_ingest(422, str(exc))


def create_app(
    store: TraceStore | None = None,
    config: LensConfig | None = None,
) -> FastAPI:
    cfg: LensConfig = config or LensConfig()
    # Store/pipeline are created lazily on first use — importing this
    # module must not create .forge-lens/ or open a DB (importers ≠ servers).
    # The lock prevents two first requests racing into duplicate connections.
    # RLock: get_pipeline calls get_store while holding the same lock.
    _init_lock = threading.RLock()
    _store_holder: dict[str, TraceStore] = {}
    _pipeline_holder: dict[str, IngestPipeline] = {}

    # Operator-config failures fail fast at app construction, never as a
    # per-request 500 — a malformed pricing_file raises here, once.
    from forge_lens.intel.pricing import PricingRegistry

    _pricing = (
        PricingRegistry.from_file(cfg.pricing_file)
        if cfg.pricing_file
        else PricingRegistry()
    )

    def get_store() -> TraceStore:
        if "db" not in _store_holder:
            with _init_lock:
                if "db" not in _store_holder:
                    _store_holder["db"] = (
                        store
                        if store is not None
                        else SQLiteTraceStore(cfg.resolved_db_path)
                    )
        return _store_holder["db"]

    def get_pipeline() -> IngestPipeline:
        if "pipeline" not in _pipeline_holder:
            with _init_lock:
                if "pipeline" not in _pipeline_holder:
                    _pipeline_holder["pipeline"] = IngestPipeline(get_store(), cfg)
        return _pipeline_holder["pipeline"]

    app = FastAPI(
        title="Forge Lens",
        version=__version__,
        description="Local-first agentic observability — ingest, store, query traces.",
    )

    # -- dependencies (auth: Cycle 08) ----------------------------------

    from forge_lens.security.keys import (
        KeyIdentity,
        hash_secret,
        project_allowed,
        role_at_least,
    )

    # effective mode: explicit auth_mode, else `api_key` implies "key",
    # else local-first "none"
    _mode = cfg.auth_mode if cfg.auth_mode != "none" else ("key" if cfg.api_key else "none")
    if _mode == "key" and not cfg.api_key:
        raise ValueError(
            "auth_mode='key' requires api_key — every write would be a "
            "permanent 401 otherwise"
        )

    def _audit(
        actor: str,
        action: str,
        resource: str,
        outcome: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Best-effort audit append — never lets logging break a request.

        Audit detail carries ids/names only; secrets never enter the
        trail (the store hashes them anyway)."""
        # suppressed deliberately: audit must never break a request —
        # a failed trail is reported via self-metrics, not exceptions
        with contextlib.suppress(Exception):
            get_store().record_audit(
                actor=actor,
                action=action,
                resource=resource,
                outcome=outcome,
                detail=detail,
                ts_ns=wall_clock_ns(),
            )

    def _resolve(x_key: str | None) -> KeyIdentity | None:
        """Header → identity. ``none`` mode = local single-user admin;
        ``key`` = the shared config secret (admin); ``keys`` = a stored
        key record (hashed lookup, role + tenant scope)."""
        if _mode == "none":
            return KeyIdentity(actor="local", role="admin", projects=())
        if _mode == "key":
            # compare as bytes: compare_digest raises TypeError on
            # non-ASCII str input — attacker input must get 401, not 500
            if cfg.api_key and x_key and secrets.compare_digest(
                x_key.encode(), cfg.api_key.encode()
            ):
                return KeyIdentity(actor="config-key", role="admin", projects=())
            return None
        # keys mode
        if not x_key:
            return None
        rec = get_store().api_key_by_hash(hash_secret(x_key))
        if rec is None:
            return None
        return KeyIdentity(
            actor=f"key:{rec['key_id']}",
            role=str(rec["role"]),
            projects=tuple(rec["projects"]),
            key_id=str(rec["key_id"]),
        )

    def _require_role(
        required: str,
    ) -> Callable[[Request, str | None], Awaitable[KeyIdentity]]:
        async def dep(
            request: Request,
            x_forge_lens_key: str | None = Header(default=None),
        ) -> KeyIdentity:
            ident = _resolve(x_forge_lens_key)
            if ident is None:
                _audit("unknown", "auth", request.url.path, "denied",
                       {"reason": "invalid_or_missing_key"})
                raise _reject_request(401, "invalid or missing API key")
            if not role_at_least(ident.role, required):
                _audit(ident.actor, "auth", request.url.path, "denied",
                       {"reason": f"role {required} required", "role": ident.role})
                raise _reject_request(403, f"role {required!r} required")
            request.state.lens_identity = ident
            return ident

        return dep

    def check_auth(
        request: Request,
        x_forge_lens_key: str | None = Header(default=None),
    ) -> None:
        """Ingest/write auth — resolves under every mode; ingest is a
        ``writer``-level action when keys are managed."""
        ident = _resolve(x_forge_lens_key)
        if ident is None:
            _audit("unknown", "auth", request.url.path, "denied",
                   {"reason": "invalid_or_missing_key"})
            raise _reject_ingest(401, "invalid or missing API key")
        if not role_at_least(ident.role, "writer"):
            _audit(ident.actor, "auth", request.url.path, "denied",
                   {"reason": "writer role required"})
            raise _reject_ingest(403, "writer role required")
        request.state.lens_identity = ident

    def _resolve_pid(request: Request, raw: str | None) -> str:
        """Resolve + scope-check in one step: the returned pid is what
        the query must use — omitting it resolves to cfg.project_id for
        check AND query, so a scoped key can never drift into a global
        (all-projects) read via the omitted-param path."""
        pid = raw or cfg.project_id
        _require_scope(request, pid)
        return pid

    def _require_scope(request: Request, project_id: str | None) -> None:
        """Tenant isolation: a project-scoped key may only touch its own
        projects. Reads AND writes — the rule is uniform."""
        ident = getattr(request.state, "lens_identity", None)
        if ident is not None and not project_allowed(ident, project_id):
            _audit(ident.actor, "scope", request.url.path, "denied",
                   {"project_id": project_id})
            raise _reject_request(
                403, f"project {project_id!r} outside key scope"
            )

    @app.middleware("http")
    async def count_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        METRICS.inc(M_API_REQUESTS)
        return await call_next(request)

    # -- ingest ---------------------------------------------------------

    @app.post(f"/{API_VERSION}/traces", dependencies=[Depends(check_auth)])
    async def ingest_native(request: Request) -> dict[str, Any]:
        """Native ingest endpoint — accepts the versioned TraceIngest contract."""
        _check_declared_size(request, cfg.max_payload_bytes)
        raw = await _read_capped_body(request, cfg.max_payload_bytes)
        try:
            body = TraceIngest.model_validate_json(raw)
        except PydanticValidationError as exc:
            raise _reject_ingest(422, str(exc)) from exc
        _require_scope(request, body.project_id or cfg.project_id)
        for trace in body.traces:
            # The envelope project fills traces that didn't name one;
            # an explicit per-trace project_id stays authoritative.
            if trace.project_id == "default" and body.project_id != "default":
                trace.project_id = body.project_id
            # the envelope source is the declared sender — fills in only
            # when the trace itself did not identify a producer
            if body.source is not None and trace.source.system == "unknown":
                trace.source = body.source
            # a trace's own project_id is authoritative — a scoped key
            # must not write into a tenant it doesn't cover
            _require_scope(request, trace.project_id)
        try:
            result = get_pipeline().ingest(body.traces, payload_bytes=len(raw))
        except (PayloadTooLargeError, RateLimitedError, ValidationError) as exc:
            raise _ingest_error(exc) from exc
        if body.sent_at is not None:
            sent_at = ensure_aware(body.sent_at)
            for trace in body.traces:
                if trace.started_at is not None and sent_at < trace.started_at:
                    result.warnings.append(
                        f"{trace.trace_id}: envelope sent_at predates trace start"
                        " — producer clock skew?"
                    )
        return {
            "accepted_spans": result.accepted_spans,
            "updated_spans": result.updated_spans,
            "duplicate_spans": result.duplicate_spans,
            "traces": result.traces,
            "sanitize": result.sanitize,
            "warnings": result.warnings,
        }

    @app.post(f"/{API_VERSION}/otlp/traces", dependencies=[Depends(check_auth)])
    async def ingest_otlp(request: Request) -> dict[str, Any]:
        """OTLP/HTTP JSON endpoint — accepts ExportTraceServiceRequest.

        ``Content-Type: application/json`` (protobuf is rejected explicitly:
        we don't claim a codec we don't ship). Malformed spans are
        quarantined and reported as warnings instead of rejecting the batch.
        """
        content_type = request.headers.get("content-type", "")
        if "protobuf" in content_type or "application/octet-stream" in content_type:
            raise _reject_ingest(
                415, "OTLP/protobuf is not supported — POST application/json"
            )
        _check_declared_size(request, cfg.max_payload_bytes)
        body = await _read_capped_body(request, cfg.max_payload_bytes)
        try:
            payload = json.loads(body)
        except Exception as exc:
            raise _reject_ingest(400, f"invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise _reject_ingest(
                422, "OTLP body must be a JSON object (ExportTraceServiceRequest)"
            )
        _reject_unencodable(payload)
        quarantine: list[str] = []
        try:
            traces = normalize_otlp_request(
                payload, project_id=cfg.project_id, errors=quarantine
            )
        except (ValidationError, PydanticValidationError) as exc:
            raise _reject_ingest(422, str(exc)) from exc
        _require_scope(request, cfg.project_id)
        for _t in traces:
            _require_scope(request, _t.project_id)
        try:
            result = get_pipeline().ingest(traces, payload_bytes=len(body))
        except (PayloadTooLargeError, RateLimitedError, ValidationError) as exc:
            raise _ingest_error(exc) from exc
        result.warnings.extend(quarantine)
        return {
            "accepted_spans": result.accepted_spans,
            "updated_spans": result.updated_spans,
            "duplicate_spans": result.duplicate_spans,
            "traces": result.traces,
            "sanitize": result.sanitize,
            "warnings": result.warnings,
        }

    # -- query ----------------------------------------------------------

    # auth_reads=true (with api_key configured) extends the same key check
    # to reads; in `keys` mode reads ALWAYS require a reader identity —
    # tenant isolation is meaningless if unauthenticated reads stay open
    read_deps = (
        [Depends(_require_role("reader"))]
        if (cfg.auth_reads or _mode == "keys")
        else []
    )

    @app.get(f"/{API_VERSION}/traces", dependencies=read_deps)
    async def list_traces(
        request: Request,
        project_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)  # symmetric with limit — clamp, don't reject
        _require_scope(request, project_id or cfg.project_id)
        with METRICS.timer(M_QUERY_LATENCY):
            items = get_store().list_traces(
                project_id=project_id or cfg.project_id,
                session_id=session_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        return {"items": items, "limit": limit, "offset": offset}

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}", dependencies=read_deps)
    async def get_trace(
        request: Request, trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        _require_scope(request, project_id or cfg.project_id)
        with METRICS.timer(M_QUERY_LATENCY):
            trace = get_store().get_trace(trace_id, project_id=project_id or cfg.project_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return trace.model_dump(mode="json")

    # -- intelligence (Cycles 03/04): graph + cost -----------------------

    def _get_trace_or_404(
        trace_id: str, project_id: str | None, request: Request
    ) -> Any:
        """Scope first — a route that forgets _require_scope still can't
        leak cross-tenant data through this choke point."""
        _require_scope(request, project_id or cfg.project_id)
        trace = get_store().get_trace(
            trace_id, project_id=project_id or cfg.project_id
        )
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return trace

    def _registry() -> Any:
        return _pricing

    @app.get(f"/{API_VERSION}/graph/traces/{{trace_id}}", dependencies=read_deps)
    async def trace_graph(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.graph import build_trace_graph

        trace = _get_trace_or_404(trace_id, project_id, request)
        return build_trace_graph(trace).model_dump(mode="json")

    @app.get(
        f"/{API_VERSION}/graph/traces/{{trace_id}}/critical-path",
        dependencies=read_deps,
    )
    async def trace_critical_path(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.graph import build_trace_graph, critical_path

        trace = _get_trace_or_404(trace_id, project_id, request)
        return critical_path(build_trace_graph(trace))

    @app.get(
        f"/{API_VERSION}/graph/traces/{{trace_id}}/bottlenecks",
        dependencies=read_deps,
    )
    async def trace_bottlenecks(request: Request,
        trace_id: str, project_id: str | None = None, top: int = 5
    ) -> dict[str, Any]:
        from forge_lens.graph import build_trace_graph, self_time_ranking

        # clamp like limit/offset — bounded query parameters are a
        # boundary concern, not the algorithm's
        top = max(0, min(top, 100))

        trace = _get_trace_or_404(trace_id, project_id, request)
        return {"items": self_time_ranking(build_trace_graph(trace), top=top)}

    @app.get(f"/{API_VERSION}/graph/sessions/{{session_id}}", dependencies=read_deps)
    async def session_graph(
        request: Request,
        session_id: str,
        project_id: str | None = None,
        limit: int = 200,
    ) -> dict[str, Any]:
        from forge_lens.graph import build_session_graph

        pid = project_id or cfg.project_id
        _require_scope(request, pid)
        lim = max(1, min(limit, 500))
        # fetch one extra row — its existence is the truncation evidence
        rows = get_store().list_traces(
            project_id=pid, session_id=session_id, limit=lim + 1
        )
        truncated = len(rows) > lim
        traces = [
            t
            for r in rows[:lim]
            if (t := get_store().get_trace(r["trace_id"], project_id=pid))
            is not None
        ]
        graph = build_session_graph(session_id, pid, traces)
        out = graph.model_dump(mode="json")
        out["stats"]["truncated"] = truncated
        return out

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/cost", dependencies=read_deps)
    async def trace_cost_report(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.intel import detect_waste, trace_cost

        trace = _get_trace_or_404(trace_id, project_id, request)
        registry = _registry()
        report = trace_cost(trace, registry)
        out = report.model_dump(mode="json")
        out["pricing_source"] = registry.source
        out["waste"] = [
            w.model_dump(mode="json") for w in detect_waste(trace, registry)
        ]
        return out

    @app.get(f"/{API_VERSION}/analytics/pricing", dependencies=read_deps)
    async def pricing_table() -> dict[str, Any]:
        registry = _registry()
        return {"source": registry.source, "models": registry.models()}

    # -- investigation (Cycle 05): failure + replay + incident -----------

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/failure", dependencies=read_deps)
    async def trace_failure(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.investigate import classify_failure

        trace = _get_trace_or_404(trace_id, project_id, request)
        return classify_failure(trace).model_dump(mode="json")

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/replay", dependencies=read_deps)
    async def trace_replay(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.investigate import replay_timeline

        trace = _get_trace_or_404(trace_id, project_id, request)
        return replay_timeline(trace)

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/incident", dependencies=read_deps)
    async def trace_incident(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.investigate import incident_bundle

        trace = _get_trace_or_404(trace_id, project_id, request)
        return incident_bundle(trace, _registry())

    @app.get(
        f"/{API_VERSION}/traces/{{trace_id}}/security", dependencies=read_deps
    )
    async def trace_security(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """MCP/tool-call security observations — observer mode, evidence only."""
        from forge_lens.security.mcp_inspect import inspect_trace

        trace = _get_trace_or_404(trace_id, project_id, request)
        return inspect_trace(trace)

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/stream", dependencies=read_deps)
    async def trace_stream(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> Any:
        """SSE replay of a stored trace — one event per span, ordered.

        Justified by need: a UI renders the waterfall progressively
        instead of waiting for the full JSON body. The trace is loaded
        once before streaming (bounded by store limits); serialization
        then happens span-by-span inside the generator.
        """
        import json as _json

        from starlette.responses import StreamingResponse

        trace = _get_trace_or_404(trace_id, project_id, request)

        def _events() -> Any:
            yield "event: meta\ndata: " + _json.dumps(
                {"trace_id": trace.trace_id, "spans": len(trace.spans)}
            ) + "\n\n"
            for sp in sorted(
                trace.spans, key=lambda x: (x.start_time, x.span_id)
            ):
                yield "event: span\ndata: " + _json.dumps(
                    sp.model_dump(mode="json"), default=str
                ) + "\n\n"
            yield "event: done\ndata: {}\n\n"

        return StreamingResponse(_events(), media_type="text/event-stream")

    # -- evaluation (Cycle 06): deterministic evidence-based scores ------

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/eval", dependencies=read_deps)
    async def trace_eval(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Default battery (no-param evaluators) for one trace."""
        from forge_lens.eval import run_evaluators

        trace = _get_trace_or_404(trace_id, project_id, request)
        results = run_evaluators(
            trace,
            [{"name": "error_free"}, {"name": "tool_success"}],
        )
        return {
            "trace_id": trace_id,
            "results": [r.model_dump(mode="json") for r in results],
        }

    _MAX_EVAL_TRACES = 500
    _MAX_EVAL_SPECS = 100

    def _eval_str(value: Any, field: str) -> str:
        """str that is safe to bind into sqlite — lone surrogates from
        WTF-8 bodies pass isinstance() but raise UnicodeEncodeError in
        the DB driver."""
        if not isinstance(value, str):
            raise _reject_request(422, f"{field} must be a string")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise _reject_request(
                422, f"{field}: unencodable string (lone surrogate)"
            ) from None
        return value

    def _reject_unencodable(obj: Any) -> None:
        """Walk a parsed payload iteratively (deep AnyValue nesting must
        not hit the recursion limit); any string that can't bind to
        SQLite TEXT is a 422 — json.loads accepts \\ud800 escapes that
        jiter (native path) would already have refused."""
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, str):
                try:
                    cur.encode("utf-8")
                except UnicodeEncodeError:
                    raise _reject_ingest(
                        422, "unencodable string (lone surrogate)"
                    ) from None
            elif isinstance(cur, dict):
                stack.extend(cur.keys())
                stack.extend(cur.values())
            elif isinstance(cur, list):
                stack.extend(cur)

    def _eval_specs(body: dict[str, Any]) -> list[Any]:
        specs = body.get("evaluators")
        if (
            not isinstance(specs, list)
            or not specs
            or len(specs) > _MAX_EVAL_SPECS
        ):
            raise _reject_request(
                422, f"evaluators: 1..{_MAX_EVAL_SPECS} specs required"
            )
        return specs

    def _eval_project_id(body: dict[str, Any], field: str = "project_id") -> str | None:
        pid = body.get("project_id")
        if pid is None:
            return None
        return _eval_str(pid, field)

    def _eval_dataset(body: dict[str, Any], field: str = "dataset") -> str | None:
        ds = body.get("dataset")
        if ds is None:
            return None
        return _eval_str(ds, field)

    def _eval_trace_ids(body: dict[str, Any], field: str = "trace_ids") -> list[str]:
        """Shape validation only — no store access."""
        trace_ids = body.get("trace_ids")
        if (
            not isinstance(trace_ids, list)
            or not trace_ids
            or len(trace_ids) > _MAX_EVAL_TRACES
        ):
            raise _reject_request(
                422, f"{field}: 1..{_MAX_EVAL_TRACES} strings required"
            )
        return [_eval_str(t, f"{field}[]") for t in trace_ids]

    def _eval_traces(
        body: dict[str, Any], request: Request, pid: str | None
    ) -> list[Any]:
        ids = _eval_trace_ids(body)
        return [_get_trace_or_404(tid, pid, request) for tid in ids]

    async def _eval_body(request: Request) -> dict[str, Any]:
        _check_declared_size(request, cfg.max_payload_bytes, _reject_request)
        try:
            body = json.loads(
                await _read_capped_body(
                    request, cfg.max_payload_bytes, _reject_request
                )
            )
        except (ValueError, RecursionError) as exc:
            # json.loads(bytes) failure classes: JSONDecodeError and
            # UnicodeDecodeError are ValueError subclasses; a plain
            # ValueError fires on ints over set_int_max_str_digits;
            # RecursionError on deeply-nested JSON — all 400, never 500.
            # NOT `except Exception`: _read_capped_body's 413
            # HTTPException lives inside this try and must propagate.
            raise _reject_request(400, f"invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise _reject_request(422, "body must be a JSON object")
        return body

    @app.post(f"/{API_VERSION}/eval/run", dependencies=read_deps)
    async def eval_run(request: Request) -> dict[str, Any]:
        """Evaluate specs over a set of stored traces → EvalReport."""
        from forge_lens.eval import evaluate_traces

        body = await _eval_body(request)
        # cheap validation first — a bad spec list or dataset field must
        # not cost a fan-out of store lookups before the 422 fires
        specs = _eval_specs(body)
        dataset = _eval_dataset(body)
        pid = _resolve_pid(request, _eval_project_id(body))
        report = evaluate_traces(
            _eval_traces(body, request, pid), specs, dataset=dataset
        )
        return report.model_dump(mode="json")

    @app.post(f"/{API_VERSION}/eval/compare", dependencies=read_deps)
    async def eval_compare(request: Request) -> dict[str, Any]:
        """A/B compare: ``{a: {trace_ids,dataset}, b: {...}, evaluators}``.

        A side may carry its own ``project_id`` — it overrides the
        top-level one for that side only, enabling cross-project A/B."""
        from forge_lens.eval import compare_runs, evaluate_traces

        body = await _eval_body(request)
        specs = _eval_specs(body)

        def _side_params(key: str) -> tuple[list[str], str | None, str | None]:
            """Shape validation only — runs for BOTH sides before any
            store fan-out, so a malformed `b` is a 422 about `b`, never
            masked by a's lookups. Errors name their side."""
            side = body.get(key)
            if not isinstance(side, dict):
                raise _reject_request(422, f"{key}: object required")
            side_ids = dict(side)
            if "project_id" not in side_ids:
                # lazy fill — an invalid top-level project_id must not
                # 422 when the side overrides it (setdefault is eager)
                side_ids["project_id"] = _eval_project_id(body)
            return (
                _eval_trace_ids(side_ids, f"{key}.trace_ids"),
                _eval_project_id(side_ids, f"{key}.project_id"),
                _eval_dataset(side, f"{key}.dataset"),
            )

        a_ids, a_pid, a_dataset = _side_params("a")
        b_ids, b_pid, b_dataset = _side_params("b")
        a_pid = _resolve_pid(request, a_pid)
        b_pid = _resolve_pid(request, b_pid)
        result = compare_runs(
            evaluate_traces(
                [_get_trace_or_404(t, a_pid, request) for t in a_ids],
                specs, dataset=a_dataset,
            ),
            evaluate_traces(
                [_get_trace_or_404(t, b_pid, request) for t in b_ids],
                specs, dataset=b_dataset,
            ),
        )
        gate = body.get("gate")
        if gate is not None:
            from forge_lens.eval.compare import apply_gate

            if not isinstance(gate, dict):
                raise _reject_request(422, "gate: object required")
            try:
                result["gate"] = apply_gate(result, gate)
            except (ValueError, TypeError) as exc:
                raise _reject_request(422, f"gate: {exc}") from exc
        return result

    @app.get(
        f"/{API_VERSION}/traces/{{trace_id}}/efficiency", dependencies=read_deps
    )
    async def trace_efficiency_route(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Cycle 12 — per-trace efficiency roll-up (tokens/tools/retries)."""
        from forge_lens.intel.efficiency import trace_efficiency

        return trace_efficiency(_get_trace_or_404(trace_id, project_id, request))

    @app.get(
        f"/{API_VERSION}/traces/{{trace_id}}/recommendations",
        dependencies=read_deps,
    )
    async def trace_recommendations(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Candidate recommendations with evidence — gains unclaimed."""
        from forge_lens.intel.recommendations import recommend

        return recommend(_get_trace_or_404(trace_id, project_id, request))

    @app.post(f"/{API_VERSION}/intel/efficiency", dependencies=read_deps)
    async def fleet_efficiency_route(request: Request) -> dict[str, Any]:
        """Fleet roll-up over a bounded window or explicit trace_ids."""
        from forge_lens.intel.efficiency import fleet_efficiency

        body = await _eval_body(request)
        store = get_store()
        pid = _resolve_pid(request, _eval_project_id(body))
        ids = None
        if "trace_ids" in body:
            ids = _eval_trace_ids(body, "trace_ids")
        if ids is not None:
            traces = [t for i in ids[:_MAX_EVAL_TRACES]
                      if (t := store.get_trace(i, project_id=pid)) is not None]
            missing = sorted(set(ids) - {t.trace_id for t in traces})
        else:
            rows = store.list_traces(project_id=pid, limit=_MAX_EVAL_TRACES)
            traces = [t for r in rows
                      if (t := store.get_trace(r["trace_id"], project_id=pid))]
            missing = []
        out = fleet_efficiency(traces)
        if missing:
            out["missing_trace_ids"] = missing
        return out

    @app.post(f"/{API_VERSION}/intel/regress", dependencies=read_deps)
    async def regress_route(request: Request) -> dict[str, Any]:
        """Cohort regression: {"a": {"trace_ids": [...]}, "b": {...}}.

        Deltas are observed differences — significance is eval.compare's
        job, not this endpoint's.
        """
        from forge_lens.intel.regressions import compare_cohorts

        body = await _eval_body(request)

        def _side(key: str) -> tuple[list[str], str | None]:
            side = body.get(key)
            if not isinstance(side, dict):
                raise _reject_request(422, f"{key}: object required")
            side_ids = dict(side)
            if "project_id" not in side_ids:
                side_ids["project_id"] = _eval_project_id(body)
            return (
                _eval_trace_ids(side_ids, f"{key}.trace_ids"),
                _eval_project_id(side_ids, f"{key}.project_id"),
            )

        a_ids, a_pid = _side("a")
        b_ids, b_pid = _side("b")
        a_pid = _resolve_pid(request, a_pid)
        b_pid = _resolve_pid(request, b_pid)
        store = get_store()

        def _cohort(ids: list[str], pid: str | None) -> list[Any]:
            return [t for i in ids if
                    (t := store.get_trace(i, project_id=pid)) is not None]

        return compare_cohorts(
            _cohort(a_ids, a_pid), _cohort(b_ids, b_pid), label="cohort"
        )

    @app.get(
        f"/{API_VERSION}/graph/traces/{{trace_id}}/analysis",
        dependencies=read_deps,
    )
    async def graph_analysis(request: Request,
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Centrality/concentration/handoff analysis over the graph."""
        from forge_lens.graph import build_trace_graph
        from forge_lens.intel.graph_intel import analyze_graph

        g = build_trace_graph(_get_trace_or_404(trace_id, project_id, request))
        return analyze_graph(g)

    # -- datasets (Cycle 06) ---------------------------------------------

    def _optional_body_datetime_ns(body: dict[str, Any], field: str) -> int | None:
        """Optional ISO ``created_at``-style field → unix ns.

        The API never reads a wall clock for stored metadata — a
        timestamp is *data*: client-supplied, or absent (NULL, honest
        "unobserved")."""
        raw = body.get(field)
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise _reject_request(422, f"{field}: ISO datetime string required")
        try:
            return to_unix_nano(ensure_aware(datetime.fromisoformat(raw)))
        except (ValueError, ValidationError) as exc:
            raise _reject_request(422, f"{field}: {exc}") from exc

    @app.put(
        f"/{API_VERSION}/datasets/{{dataset_id}}",
        dependencies=[Depends(check_auth)],
    )
    async def put_dataset(dataset_id: str, request: Request) -> dict[str, Any]:
        body = await _eval_body(request)
        name = _eval_str(body.get("name"), "name")
        if not name.strip():
            raise _reject_request(422, "name: non-empty string required")
        task = body.get("task")
        if task is not None:
            task = _eval_str(task, "task")
        metadata = body.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise _reject_request(422, "metadata: object required")
        created_ns = _optional_body_datetime_ns(body, "created_at")
        pid = _resolve_pid(request, _eval_project_id(body))
        get_store().upsert_dataset(
            dataset_id,
            project_id=pid,
            name=name.strip(),
            task=task,
            metadata=metadata,
            now_ns=created_ns,
        )
        return {"dataset_id": dataset_id, "project_id": pid, "stored": True}

    @app.get(f"/{API_VERSION}/datasets", dependencies=read_deps)
    async def list_datasets(
        request: Request, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        pid = _resolve_pid(request, project_id)
        return get_store().list_datasets(pid)

    @app.get(f"/{API_VERSION}/datasets/{{dataset_id}}", dependencies=read_deps)
    async def get_dataset(
        request: Request, dataset_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        pid = _resolve_pid(request, project_id)
        ds = get_store().get_dataset(dataset_id, pid)
        if ds is None:
            raise _reject_request(404, f"dataset {dataset_id!r} not found")
        return ds

    @app.put(
        f"/{API_VERSION}/datasets/{{dataset_id}}/versions/{{version}}",
        dependencies=[Depends(check_auth)],
    )
    async def put_dataset_version(
        dataset_id: str, version: str, request: Request
    ) -> dict[str, Any]:
        body = await _eval_body(request)
        pid = _resolve_pid(request, _eval_project_id(body))
        if get_store().get_dataset(dataset_id, pid) is None:
            raise _reject_request(
                404, f"dataset {dataset_id!r} not found — create it first"
            )
        if get_store().get_dataset_version(dataset_id, version, pid) is not None:
            raise _reject_request(
                409, f"dataset version {dataset_id!r}@{version!r} exists — "
                     "versions are append-only, never mutated in place"
            )
        cases = body.get("cases")
        if not isinstance(cases, list) or any(
            not isinstance(c, dict) or not str(c.get("case_id") or "").strip()
            for c in cases
        ):
            raise _reject_request(
                422, "cases: list of objects each with a non-empty case_id"
            )
        for c in cases:
            _eval_str(c["case_id"], "cases[].case_id")
        description = body.get("description")
        if description is not None:
            description = _eval_str(description, "description")
        metadata = body.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise _reject_request(422, "metadata: object required")
        n = get_store().upsert_dataset_version(
            dataset_id,
            version,
            project_id=pid,
            description=description,
            cases=cases,
            metadata=metadata,
            now_ns=_optional_body_datetime_ns(body, "created_at"),
        )
        return {"dataset_id": dataset_id, "version": version, "cases": n}

    @app.get(
        f"/{API_VERSION}/datasets/{{dataset_id}}/versions",
        dependencies=read_deps,
    )
    async def list_dataset_versions(
        request: Request, dataset_id: str, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        pid = _resolve_pid(request, project_id)
        if get_store().get_dataset(dataset_id, pid) is None:
            raise _reject_request(404, f"dataset {dataset_id!r} not found")
        return get_store().list_dataset_versions(dataset_id, pid)

    @app.get(
        f"/{API_VERSION}/datasets/{{dataset_id}}/versions/{{version}}",
        dependencies=read_deps,
    )
    async def get_dataset_version(
        request: Request, dataset_id: str, version: str,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        pid = _resolve_pid(request, project_id)
        v = get_store().get_dataset_version(dataset_id, version, pid)
        if v is None:
            raise _reject_request(
                404, f"dataset version {dataset_id!r}@{version!r} not found"
            )
        return v

    # -- admin (Cycle 08: keys, audit, retention) ------------------------

    admin_deps = [Depends(_require_role("admin"))]

    @app.post(f"/{API_VERSION}/admin/keys", dependencies=admin_deps)
    async def admin_create_key(request: Request) -> dict[str, Any]:
        """Issue a managed key. The secret is returned exactly once —
        only its hash is stored."""
        from forge_lens.security.keys import ROLES, generate_key

        body = await _eval_body(request)
        name = _eval_str(body.get("name"), "name")
        role = body.get("role")
        projects = body.get("projects")
        if not name.strip():
            raise _reject_request(422, "name: non-empty string required")
        if not isinstance(role, str) or role not in ROLES:
            raise _reject_request(422, f"role: one of {ROLES}")
        if projects is not None and (
            not isinstance(projects, list)
            or any(not isinstance(p, str) for p in projects)
        ):
            raise _reject_request(422, "projects: list of strings required")
        ident = getattr(request.state, "lens_identity", None)
        # a scoped admin may only mint keys INSIDE its own scope —
        # never unscoped (projects=None/[]) and never a foreign
        # project; otherwise key creation is a tenant escape hatch
        if (
            ident is not None and ident.projects
            and (not projects
                 or any(p not in ident.projects for p in projects))
        ):
            raise _reject_request(
                403, "requested projects outside caller scope"
            )
        key_id, secret, hashed = generate_key()
        get_store().create_api_key(
            key_id,
            name=name.strip(),
            key_hash=hashed,
            role=role,
            projects=projects,
            now_ns=wall_clock_ns(),
        )
        _audit(
            ident.actor if ident else "unknown",
            "key.create",
            key_id,
            "ok",
            {"name": name.strip(), "role": role},
        )
        return {
            "key_id": key_id,
            "secret": secret,
            "role": role,
            "projects": projects or [],
            "warning": "the secret is shown once — only its hash is stored",
        }

    @app.get(f"/{API_VERSION}/admin/keys", dependencies=admin_deps)
    async def admin_list_keys(request: Request) -> list[dict[str, Any]]:
        keys = get_store().list_api_keys()
        ident = getattr(request.state, "lens_identity", None)
        if ident is not None and ident.projects:
            # scoped admins see only keys fully inside their scope
            keys = [k for k in keys
                    if k["projects"]
                    and all(p in ident.projects for p in k["projects"])]
        return keys

    @app.delete(f"/{API_VERSION}/admin/keys/{{key_id}}", dependencies=admin_deps)
    async def admin_revoke_key(request: Request, key_id: str) -> dict[str, Any]:
        ident = getattr(request.state, "lens_identity", None)
        if ident is not None and ident.projects:
            target = next(
                (k for k in get_store().list_api_keys()
                 if k["key_id"] == key_id), None,
            )
            if target is None:
                raise _reject_request(
                    404, f"key {key_id!r} not found or already revoked")
            if (not target["projects"]
                    or any(p not in ident.projects
                           for p in target["projects"])):
                raise _reject_request(
                    403, "key outside caller scope")
        if not get_store().revoke_api_key(key_id, now_ns=wall_clock_ns()):
            raise _reject_request(404, f"key {key_id!r} not found or already revoked")
        ident = getattr(request.state, "lens_identity", None)
        _audit(
            ident.actor if ident else "unknown",
            "key.revoke", key_id, "ok",
        )
        return {"key_id": key_id, "revoked": True}

    @app.get(f"/{API_VERSION}/admin/audit", dependencies=admin_deps)
    async def admin_audit(
        request: Request,
        action: str | None = None,
        actor: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        # audit events carry no reliable per-project attribution —
        # scoped admins would see (or miss) cross-tenant noise, so the
        # trail is restricted to unscoped admins entirely
        ident = getattr(request.state, "lens_identity", None)
        if ident is not None and ident.projects:
            raise _reject_request(
                403, "audit trail requires an unscoped admin key")
        if limit > 500 or limit < 0:
            raise _reject_request(422, "limit: 0..500")
        return get_store().list_audit(
            action=action, actor=actor, limit=limit, offset=offset
        )

    @app.post(f"/{API_VERSION}/admin/prune", dependencies=admin_deps)
    async def admin_prune(request: Request) -> dict[str, Any]:
        """Retention enforcement — delete traces older than a cutoff.

        ``before`` (ISO) or ``older_than_days`` selects the cutoff;
        ``project_id`` bounds the prune to one tenant."""
        body = await _eval_body(request)
        before = body.get("before")
        older_days = body.get("older_than_days")
        pid = _resolve_pid(request, _eval_project_id(body))
        if before is not None and older_days is not None:
            raise _reject_request(422, "before and older_than_days are exclusive")
        if before is not None:
            cutoff = _optional_body_datetime_ns(body, "before")
            if cutoff is None:
                raise _reject_request(422, "before: ISO datetime required")
        elif older_days is not None:
            if (
                not isinstance(older_days, (int, float))
                or isinstance(older_days, bool)
                or not math.isfinite(older_days)
                or older_days < 0
                or older_days > 106_000  # int64 ns ceiling ≈ 106,751 days
            ):
                raise _reject_request(
                    422, "older_than_days: finite number 0..106000 required")
            cutoff = wall_clock_ns() - int(older_days * 86_400_000_000_000)
        else:
            raise _reject_request(422, "before or older_than_days required")
        deleted = get_store().prune_traces(
            before_ns=cutoff, project_id=pid
        )
        ident = getattr(request.state, "lens_identity", None)
        _audit(
            ident.actor if ident else "unknown",
            "traces.prune",
            pid or "*",
            "ok",
            {"before_ns": cutoff, **deleted},
        )
        return {"deleted": deleted, "before_ns": cutoff}

    # -- agentic ops (Cycle 07): loops, specialists, alerts, dashboard --

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/loops", dependencies=read_deps)
    async def trace_loops(request: Request,
        trace_id: str,
        project_id: str | None = None,
        repeat_threshold: int = 3,
    ) -> dict[str, Any]:
        """Heuristic loop/no-progress findings with span evidence."""
        from forge_lens.ops.loops import detect_loops

        trace = _get_trace_or_404(trace_id, project_id, request)
        return detect_loops(trace, repeat_threshold=max(1, min(50, repeat_threshold)))

    @app.get(f"/{API_VERSION}/analyze/specialists", dependencies=read_deps)
    async def list_specialists() -> dict[str, Any]:
        from forge_lens.ops.agents import SPECIALISTS

        return {"specialists": list(SPECIALISTS)}

    @app.post(
        f"/{API_VERSION}/analyze/{{specialist}}/{{trace_id}}",
        dependencies=read_deps,
    )
    async def run_analysis(
        specialist: str, trace_id: str, request: Request
    ) -> dict[str, Any]:
        """Run a registered specialist under explicit budgets.

        Body: {"budget": {...}, "context": {...}, "project_id": "..."}
        (all fields optional; body itself must be a JSON object).
        Budgets are cooperative — timeout_s bounds iterations, not
        preemption mid-iteration; execution runs in a worker thread so
        a slow pass does not stall other requests.
        """
        from forge_lens.ops.agents import AgentBudget, run_specialist

        body = await _eval_body(request)
        b = body.get("budget") or {}
        if not isinstance(b, dict):
            raise _reject_request(422, "budget must be an object")

        def _num(field: str, default: float, cast: Any) -> Any:
            v = b.get(field, default)
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise _reject_request(422, f"budget.{field} must be a number")
            try:
                out = cast(v)
            except (TypeError, ValueError, OverflowError):
                raise _reject_request(
                    422, f"budget.{field} must be a number"
                ) from None
            if (
                isinstance(out, (int, float))
                and (not math.isfinite(out) or out <= 0)
            ):
                raise _reject_request(
                    422, f"budget.{field} must be a positive finite number")
            return out

        budget = AgentBudget(
            max_iterations=_num("max_iterations", 8, int),
            token_budget=_num("token_budget", 200_000, int),
            timeout_s=_num("timeout_s", 30.0, float),
        )
        ctx = body.get("context") or {}
        if not isinstance(ctx, dict):
            raise _reject_request(422, "context must be an object")
        # server-side objects can never arrive over JSON — inject so the
        # specialist gets the real registry, not a dead/invalid wire value
        ctx = {**ctx, "pricing": _registry()}
        run_id = body.get("run_id")
        if run_id is not None and not isinstance(run_id, str):
            raise _reject_request(422, "run_id must be a string")
        trace = _get_trace_or_404(
            trace_id, _eval_project_id(body), request
        )
        try:
            run = await run_in_threadpool(
                run_specialist,
                specialist, trace, budget=budget, context=ctx,
                run_id=run_id or f"{specialist}:{trace_id}",
            )
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            # a malformed context value must not 500 — the boundary owns it
            raise _reject_request(
                422, f"specialist rejected context: {exc}"
            ) from None
        return {
            "run_id": run.run_id, "specialist": run.specialist,
            "stop_reason": run.stop_reason, "iterations": run.iterations,
            "tokens_observed": run.tokens_observed,
            "cancelled": run.cancelled, "findings": run.findings,
        }

    @app.post(f"/{API_VERSION}/alerts/evaluate", dependencies=read_deps)
    async def alerts_evaluate(request: Request) -> dict[str, Any]:
        """Evaluate threshold rules inline (rules + optional project_id).

        Metrics are computed from the store at request time — no clock.
        """
        from forge_lens.ops.alerts import evaluate_rules

        body = await _eval_body(request)
        raw_rules = body.get("rules")
        if raw_rules is not None and not isinstance(raw_rules, list):
            raise _reject_request(422, "rules must be a list of <=100 objects")
        pid = _resolve_pid(request, _eval_project_id(body))
        rules: list[Any] = list(raw_rules or [])
        if body.get("use_saved"):
            from forge_lens.ops.alerts import load_rules

            rules += load_rules(cfg.data_dir / "alert-rules.json")
        if len(rules) > 100:
            raise _reject_request(422, "rules must be a list of <=100 objects")
        stats = get_store().stats(project_id=pid)
        spans_total = int(stats.get("spans") or 0)
        errors = int(stats.get("error_spans") or 0)
        metrics = {
            "span_count": float(spans_total),
            "trace_count": float(stats.get("traces") or 0),
            "error_count": float(errors),
            "error_rate": errors / spans_total if spans_total else 0.0,
        }
        out = evaluate_rules([r for r in rules if isinstance(r, dict)], metrics)
        out["project_id"] = pid
        return out

    @app.post(f"/{API_VERSION}/alerts/rules", dependencies=admin_deps)
    async def alerts_save_rule(request: Request) -> dict[str, Any]:
        """Persist a rule to <data_dir>/alert-rules.json — durable, local.

        Saved rules are merged into every ``/alerts/evaluate`` call that
        passes ``"use_saved": true``."""
        from forge_lens.ops.alerts import save_rule

        ident = getattr(request.state, "lens_identity", None)
        if ident is not None and ident.projects:
            raise _reject_request(
                403, "alert rules are global — an unscoped admin key "
                     "is required")
        body = await _eval_body(request)
        rule = body.get("rule")
        if not isinstance(rule, dict) or not isinstance(
            rule.get("rule_id"), str
        ):
            raise _reject_request(
                422, "rule: object with a string rule_id required")
        path = cfg.data_dir / "alert-rules.json"
        rules = save_rule(path, rule)
        ident = getattr(request.state, "lens_identity", None)
        _audit(ident.actor if ident else "anon",
               "alert_rule.save", str(rule["rule_id"]), "ok", {})
        return {"rules": rules, "path": str(path)}

    @app.delete(
        f"/{API_VERSION}/alerts/rules/{{rule_id}}", dependencies=admin_deps
    )
    async def alerts_delete_rule(request: Request, rule_id: str) -> dict[str, Any]:
        """Delete a persisted rule — keeps the file under the eval cap."""
        from forge_lens.ops.alerts import delete_rule, load_rules

        ident = getattr(request.state, "lens_identity", None)
        if ident is not None and ident.projects:
            raise _reject_request(
                403, "alert rules are global — an unscoped admin key "
                     "is required")
        path = cfg.data_dir / "alert-rules.json"
        if not any(r["rule_id"] == rule_id for r in load_rules(path)):
            raise _reject_request(404, "rule not found")
        rules = delete_rule(path, rule_id)
        ident = getattr(request.state, "lens_identity", None)
        _audit(ident.actor if ident else "anon",
               "alert_rule.delete", rule_id, "ok", {})
        return {"rules": rules}

    @app.get(f"/{API_VERSION}/ops/summary", dependencies=read_deps)
    async def ops_summary(
        request: Request, project_id: str | None = None
    ) -> dict[str, Any]:
        """Operational dashboard data: store stats + recent failures +
        cost/token roll-up over the bounded recent window."""
        store = get_store()
        pid = _resolve_pid(request, project_id)
        traces = store.list_traces(project_id=pid, limit=200)
        failed = [t for t in traces if t.get("status") == "error"]
        return {
            "project_id": pid,
            "stats": store.stats(project_id=pid),
            "recent_traces": len(traces),
            "recent_failures": sorted(
                (t["trace_id"] for t in failed), reverse=True
            )[:50],
            "note": "bounded window (200 traces) — not a fleet-wide total",
        }

    # -- ops ------------------------------------------------------------

    @app.get(f"/{API_VERSION}/capabilities", dependencies=read_deps)
    async def capabilities() -> dict[str, Any]:
        """Host adapter capability matrix — honest coverage claims."""
        from forge_lens.adapters.capabilities import capability_matrix

        return capability_matrix()

    @app.get(f"/{API_VERSION}/health", dependencies=read_deps)
    async def health(request: Request) -> dict[str, Any]:
        ident = getattr(request.state, "lens_identity", None)
        projects = getattr(ident, "projects", None)
        scoped = sorted(projects) if projects else None
        if scoped is not None and len(scoped) == 1:
            stats: dict[str, Any] | None = get_store().stats(
                project_id=scoped[0])
        elif scoped:
            stats = None  # multi-project key: no per-project fan-in here
        else:
            stats = get_store().stats()
        out: dict[str, Any] = {
            "status": "ok",
            "version": __version__,
            "storage": "sqlite",
        }
        if stats is not None:
            out["stats"] = stats
        return out

    @app.get(f"/{API_VERSION}/metrics", dependencies=read_deps)
    async def metrics() -> dict[str, Any]:
        return METRICS.snapshot()

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    return app


def make_default_app() -> FastAPI:
    """App factory used by uvicorn/CLI: config from env + .forge-lens/."""
    from forge_lens.core.config import load_config

    config = load_config()
    return create_app(config=config)


def __getattr__(name: str) -> Any:
    """Lazily build the module-level ``app`` for ``uvicorn forge_lens.api.app:app``.

    Deferring construction keeps ``import forge_lens.api.app`` free of
    config/DB side effects (importers ≠ servers).
    """
    if name == "app":
        return make_default_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
