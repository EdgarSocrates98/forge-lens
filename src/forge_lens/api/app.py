"""FastAPI application — OTLP endpoint, native ingest, query endpoints.

The API is a thin boundary: all real work lives in the pipeline/store.
The engine itself has no network dependency; this module is the only
place where HTTP is handled.
"""

from __future__ import annotations

import json
import secrets
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import ValidationError as PydanticValidationError

from forge_lens import __version__
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
from forge_lens.core.time import ensure_aware
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

    # -- dependencies ---------------------------------------------------

    def check_auth(x_forge_lens_key: str | None = Header(default=None)) -> None:
        """Optional ingestion auth: only enforced when api_key is configured."""
        # compare as bytes: compare_digest raises TypeError on non-ASCII
        # str input — an attacker-controlled header must get 401, not 500
        if cfg.api_key and not secrets.compare_digest(
            (x_forge_lens_key or "").encode(), cfg.api_key.encode()
        ):
            METRICS.inc(M_API_ERRORS)
            raise HTTPException(status_code=401, detail="invalid or missing API key")

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

        for trace in body.traces:
            # The envelope project fills traces that didn't name one;
            # an explicit per-trace project_id stays authoritative.
            if trace.project_id == "default" and body.project_id != "default":
                trace.project_id = body.project_id
            # the envelope source is the declared sender — fills in only
            # when the trace itself did not identify a producer
            if body.source is not None and trace.source.system == "unknown":
                trace.source = body.source
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
        quarantine: list[str] = []
        try:
            traces = normalize_otlp_request(
                payload, project_id=cfg.project_id, errors=quarantine
            )
        except (ValidationError, PydanticValidationError) as exc:
            raise _reject_ingest(422, str(exc)) from exc
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
    # to reads; default keeps local-first reads open
    read_deps = [Depends(check_auth)] if cfg.auth_reads else []

    @app.get(f"/{API_VERSION}/traces", dependencies=read_deps)
    async def list_traces(
        project_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        offset = max(0, offset)  # symmetric with limit — clamp, don't reject
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
    async def get_trace(trace_id: str, project_id: str | None = None) -> dict[str, Any]:
        with METRICS.timer(M_QUERY_LATENCY):
            trace = get_store().get_trace(trace_id, project_id=project_id or cfg.project_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return trace.model_dump(mode="json")

    # -- intelligence (Cycles 03/04): graph + cost -----------------------

    def _get_trace_or_404(trace_id: str, project_id: str | None) -> Any:
        trace = get_store().get_trace(
            trace_id, project_id=project_id or cfg.project_id
        )
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return trace

    def _registry() -> Any:
        return _pricing

    @app.get(f"/{API_VERSION}/graph/traces/{{trace_id}}", dependencies=read_deps)
    async def trace_graph(
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.graph import build_trace_graph

        trace = _get_trace_or_404(trace_id, project_id)
        return build_trace_graph(trace).model_dump(mode="json")

    @app.get(
        f"/{API_VERSION}/graph/traces/{{trace_id}}/critical-path",
        dependencies=read_deps,
    )
    async def trace_critical_path(
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.graph import build_trace_graph, critical_path

        trace = _get_trace_or_404(trace_id, project_id)
        return critical_path(build_trace_graph(trace))

    @app.get(
        f"/{API_VERSION}/graph/traces/{{trace_id}}/bottlenecks",
        dependencies=read_deps,
    )
    async def trace_bottlenecks(
        trace_id: str, project_id: str | None = None, top: int = 5
    ) -> dict[str, Any]:
        from forge_lens.graph import build_trace_graph, self_time_ranking

        # clamp like limit/offset — bounded query parameters are a
        # boundary concern, not the algorithm's
        top = max(0, min(top, 100))

        trace = _get_trace_or_404(trace_id, project_id)
        return {"items": self_time_ranking(build_trace_graph(trace), top=top)}

    @app.get(f"/{API_VERSION}/graph/sessions/{{session_id}}", dependencies=read_deps)
    async def session_graph(
        session_id: str, project_id: str | None = None, limit: int = 200
    ) -> dict[str, Any]:
        from forge_lens.graph import build_session_graph

        pid = project_id or cfg.project_id
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
    async def trace_cost_report(
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.intel import detect_waste, trace_cost

        trace = _get_trace_or_404(trace_id, project_id)
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
    async def trace_failure(
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.investigate import classify_failure

        trace = _get_trace_or_404(trace_id, project_id)
        return classify_failure(trace).model_dump(mode="json")

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/replay", dependencies=read_deps)
    async def trace_replay(
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.investigate import replay_timeline

        trace = _get_trace_or_404(trace_id, project_id)
        return replay_timeline(trace)

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/incident", dependencies=read_deps)
    async def trace_incident(
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        from forge_lens.investigate import incident_bundle

        trace = _get_trace_or_404(trace_id, project_id)
        return incident_bundle(trace, _registry())

    # -- evaluation (Cycle 06): deterministic evidence-based scores ------

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}/eval", dependencies=read_deps)
    async def trace_eval(
        trace_id: str, project_id: str | None = None
    ) -> dict[str, Any]:
        """Default battery (no-param evaluators) for one trace."""
        from forge_lens.eval import run_evaluators

        trace = _get_trace_or_404(trace_id, project_id)
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

    def _eval_project_id(body: dict[str, Any]) -> str | None:
        pid = body.get("project_id")
        if pid is None:
            return None
        return _eval_str(pid, "project_id")

    def _eval_dataset(body: dict[str, Any]) -> str | None:
        ds = body.get("dataset")
        if ds is None:
            return None
        return _eval_str(ds, "dataset")

    def _eval_trace_ids(body: dict[str, Any]) -> list[str]:
        """Shape validation only — no store access."""
        trace_ids = body.get("trace_ids")
        if (
            not isinstance(trace_ids, list)
            or not trace_ids
            or len(trace_ids) > _MAX_EVAL_TRACES
        ):
            raise _reject_request(
                422, f"trace_ids: 1..{_MAX_EVAL_TRACES} strings required"
            )
        return [_eval_str(t, "trace_ids[]") for t in trace_ids]

    def _eval_traces(body: dict[str, Any]) -> list[Any]:
        ids = _eval_trace_ids(body)
        pid = _eval_project_id(body)
        return [_get_trace_or_404(tid, pid) for tid in ids]

    async def _eval_body(request: Request) -> Any:
        _check_declared_size(request, cfg.max_payload_bytes, _reject_request)
        try:
            body = json.loads(
                await _read_capped_body(
                    request, cfg.max_payload_bytes, _reject_request
                )
            )
        except json.JSONDecodeError as exc:
            raise _reject_request(400, f"invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise _reject_request(422, "body must be a JSON object")
        return body

    @app.post(f"/{API_VERSION}/eval/run", dependencies=read_deps)
    async def eval_run(request: Request) -> dict[str, Any]:
        """Evaluate specs over a set of stored traces → EvalReport."""
        from forge_lens.eval import evaluate_traces

        body = await _eval_body(request)
        # cheap validation first — a bad spec list must not cost a
        # fan-out of store lookups before the 422 fires
        specs = _eval_specs(body)
        report = evaluate_traces(
            _eval_traces(body), specs, dataset=_eval_dataset(body)
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

        def _side_params(key: str) -> Any:
            """Shape validation only — runs for BOTH sides before any
            store fan-out, so a malformed `b` is a 422 about `b`, never
            masked by a's lookups."""
            side = body.get(key)
            if not isinstance(side, dict):
                raise _reject_request(422, f"{key}: object required")
            side_ids = dict(side)
            side_ids.setdefault("project_id", _eval_project_id(body))
            return (
                _eval_trace_ids(side_ids),
                _eval_project_id(side_ids),
                _eval_dataset(side),
            )

        a_ids, a_pid, a_dataset = _side_params("a")
        b_ids, b_pid, b_dataset = _side_params("b")
        return compare_runs(
            evaluate_traces(
                [_get_trace_or_404(t, a_pid) for t in a_ids],
                specs, dataset=a_dataset,
            ),
            evaluate_traces(
                [_get_trace_or_404(t, b_pid) for t in b_ids],
                specs, dataset=b_dataset,
            ),
        )

    # -- ops ------------------------------------------------------------

    @app.get(f"/{API_VERSION}/health", dependencies=read_deps)
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "storage": "sqlite",
            "stats": get_store().stats(),
        }

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
