"""FastAPI application — OTLP endpoint, native ingest, query endpoints.

The API is a thin boundary: all real work lives in the pipeline/store.
The engine itself has no network dependency; this module is the only
place where HTTP is handled.
"""

from __future__ import annotations

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


def _check_declared_size(request: Request, max_bytes: int) -> None:
    """Fast-path 413 on Content-Length before buffering the body.

    Doesn't replace the post-read check — chunked bodies may lack the
    header and a client can lie low — but it keeps the common oversized
    case from being fully buffered into memory first.
    """
    cl = request.headers.get("content-length")
    if cl is not None and cl.isdigit() and int(cl) > max_bytes:
        raise _reject_ingest(413, "payload too large")


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
        raw = await request.body()
        if len(raw) > cfg.max_payload_bytes:
            raise _reject_ingest(413, "payload too large")
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
        body = await request.body()
        if len(body) > cfg.max_payload_bytes:
            raise _reject_ingest(413, "payload too large")
        try:
            payload = await request.json()
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
            "warnings": result.warnings,
        }

    # -- query ----------------------------------------------------------

    @app.get(f"/{API_VERSION}/traces")
    async def list_traces(
        project_id: str | None = None,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        with METRICS.timer(M_QUERY_LATENCY):
            items = get_store().list_traces(
                project_id=project_id or cfg.project_id,
                session_id=session_id,
                status=status,
                limit=limit,
                offset=offset,
            )
        return {"items": items, "limit": limit, "offset": offset}

    @app.get(f"/{API_VERSION}/traces/{{trace_id}}")
    async def get_trace(trace_id: str, project_id: str | None = None) -> dict[str, Any]:
        with METRICS.timer(M_QUERY_LATENCY):
            trace = get_store().get_trace(trace_id, project_id=project_id or cfg.project_id)
        if trace is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return trace.model_dump(mode="json")

    # -- ops ------------------------------------------------------------

    @app.get(f"/{API_VERSION}/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "storage": "sqlite",
            "stats": get_store().stats(),
        }

    @app.get(f"/{API_VERSION}/metrics")
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
