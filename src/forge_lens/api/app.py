"""FastAPI application — OTLP endpoint, native ingest, query endpoints.

The API is a thin boundary: all real work lives in the pipeline/store.
The engine itself has no network dependency; this module is the only
place where HTTP is handled.
"""

from __future__ import annotations

import secrets
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
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.otlp.normalize import normalize_otlp_request
from forge_lens.storage.base import TraceStore
from forge_lens.storage.sqlite import SQLiteTraceStore

API_VERSION = "v1"


def _ingest_error(exc: Exception) -> HTTPException:
    METRICS.inc(M_API_ERRORS)
    if isinstance(exc, PayloadTooLargeError):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, RateLimitedError):
        return HTTPException(status_code=429, detail=str(exc))
    METRICS.inc(M_INGEST_ERRORS)
    return HTTPException(status_code=422, detail=str(exc))


def create_app(
    store: TraceStore | None = None,
    config: LensConfig | None = None,
) -> FastAPI:
    cfg: LensConfig = config or LensConfig()
    # Store/pipeline are created lazily on first use — importing this
    # module must not create .forge-lens/ or open a DB (importers ≠ servers).
    _store_holder: dict[str, TraceStore] = {}
    _pipeline_holder: dict[str, IngestPipeline] = {}

    def get_store() -> TraceStore:
        if "db" not in _store_holder:
            _store_holder["db"] = (
                store if store is not None else SQLiteTraceStore(cfg.resolved_db_path)
            )
        return _store_holder["db"]

    def get_pipeline() -> IngestPipeline:
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
        if cfg.api_key and not secrets.compare_digest(
            x_forge_lens_key or "", cfg.api_key
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
        raw = await request.body()
        if len(raw) > cfg.max_payload_bytes:
            METRICS.inc(M_API_ERRORS)
            raise HTTPException(status_code=413, detail="payload too large")
        try:
            body = TraceIngest.model_validate_json(raw)
        except PydanticValidationError as exc:
            METRICS.inc(M_API_ERRORS)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

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
            for trace in body.traces:
                if trace.started_at is not None and body.sent_at < trace.started_at:
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
        body = await request.body()
        if len(body) > cfg.max_payload_bytes:
            METRICS.inc(M_API_ERRORS)
            raise HTTPException(status_code=413, detail="payload too large")
        try:
            payload = await request.json()
        except Exception as exc:
            METRICS.inc(M_API_ERRORS)
            raise HTTPException(status_code=400, detail=f"invalid JSON: {exc}") from exc
        quarantine: list[str] = []
        try:
            traces = normalize_otlp_request(
                payload, project_id=cfg.project_id, errors=quarantine
            )
        except (ValidationError, PydanticValidationError) as exc:
            METRICS.inc(M_API_ERRORS)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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
