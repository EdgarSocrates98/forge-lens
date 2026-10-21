"""FastAPI application — OTLP endpoint, native ingest, query endpoints.

The API is a thin boundary: all real work lives in the pipeline/store.
The engine itself has no network dependency; this module is the only
place where HTTP is handled.
"""

from __future__ import annotations

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
from forge_lens.core.metrics import M_API_ERRORS, M_API_REQUESTS, METRICS
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.otlp.normalize import normalize_otlp_request
from forge_lens.storage.base import TraceStore
from forge_lens.storage.sqlite import SQLiteTraceStore

API_VERSION = "v1"


def create_app(
    store: TraceStore | None = None,
    config: LensConfig | None = None,
) -> FastAPI:
    cfg: LensConfig = config or LensConfig()
    # Store is created lazily on first use — importing this module must not
    # create .forge-lens/ or open a DB (importers ≠ servers).
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
        if cfg.api_key and x_forge_lens_key != cfg.api_key:
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
    async def ingest_native(body: TraceIngest) -> dict[str, Any]:
        """Native ingest endpoint — accepts the versioned TraceIngest contract."""
        for trace in body.traces:
            trace.project_id = body.project_id
        try:
            result = get_pipeline().ingest(body.traces)
        except (PayloadTooLargeError, RateLimitedError) as exc:
            METRICS.inc(M_API_ERRORS)
            status = 413 if isinstance(exc, PayloadTooLargeError) else 429
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {
            "accepted_spans": result.accepted_spans,
            "duplicate_spans": result.duplicate_spans,
            "traces": result.traces,
            "sanitize": result.sanitize,
            "warnings": result.warnings,
        }

    @app.post(f"/{API_VERSION}/otlp/traces", dependencies=[Depends(check_auth)])
    async def ingest_otlp(request: Request) -> dict[str, Any]:
        """OTLP/HTTP JSON endpoint — accepts ExportTraceServiceRequest.

        ``Content-Type: application/json`` (protobuf is rejected explicitly:
        we don't claim a codec we don't ship).
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
        try:
            traces = normalize_otlp_request(payload, project_id=cfg.project_id)
        except (ValidationError, PydanticValidationError) as exc:
            METRICS.inc(M_API_ERRORS)
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            result = get_pipeline().ingest(traces, payload_bytes=len(body))
        except (PayloadTooLargeError, RateLimitedError) as exc:
            METRICS.inc(M_API_ERRORS)
            status = 413 if isinstance(exc, PayloadTooLargeError) else 429
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        return {
            "accepted_spans": result.accepted_spans,
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


app = make_default_app()
