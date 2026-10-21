"""``forge-lens`` CLI — init, serve, ingest, inspect, doctor.

Local-first: every command works offline against the local store.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from forge_lens import __version__
from forge_lens.contracts.v1.trace import Span
from forge_lens.core.config import LensConfig, init_project, load_config
from forge_lens.storage.sqlite import SQLiteTraceStore

app = typer.Typer(
    name="forge-lens",
    help="Local-first agentic observability: ingest, store and query agent traces.",
    no_args_is_help=True,
)
traces_app = typer.Typer(help="Inspect stored traces.", no_args_is_help=True)
app.add_typer(traces_app, name="traces")


def _config(db: Path | None = None) -> LensConfig:
    base = load_config()
    if db is not None:
        return LensConfig(**{**base.__dict__, "db_path": db})
    return base


def _store(cfg: LensConfig) -> SQLiteTraceStore:
    return SQLiteTraceStore(cfg.resolved_db_path)


_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _warn_if_exposed(host: str, cfg: LensConfig) -> None:
    """Binding non-loopback exposes the API — warn about what's reachable."""
    if host.lower() in _LOOPBACK:
        return
    if not cfg.api_key:
        typer.echo(
            f"warning: binding {host} with no api_key — all trace data is "
            "readable and ingest is open to anyone on the network. "
            "Set FORGE_LENS_API_KEY or api_key in config.toml.",
            err=True,
        )
    else:
        typer.echo(
            f"warning: binding {host} — api_key protects ingest only; "
            "GET /v1/traces, /v1/health and /v1/metrics stay unauthenticated "
            "by design (local-first reads).",
            err=True,
        )


def _version(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    return None


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path(),
    project_id: Annotated[str, typer.Option("--project", "-p")] = "default",
) -> None:
    """Initialize .forge-lens/ in a project directory."""
    cfg = init_project(path.resolve(), project_id)
    typer.echo(f"Initialized Forge Lens project at {cfg.parent}")
    typer.echo(f"  config: {cfg}")
    typer.echo(f"  db:     {cfg.parent / 'lens.db'} (created on first ingest)")


@app.command()
def serve(
    host: Annotated[
        str | None, typer.Option("--host", help="defaults to the endpoint config")
    ] = None,
    port: Annotated[
        int | None, typer.Option("--port", "-p", help="defaults to the endpoint config")
    ] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Run the Forge Lens ingestion + query API.

    Bind defaults come from the configured ``endpoint``
    (``.forge-lens/config.toml`` / ``FORGE_LENS_ENDPOINT``); ``--host``
    and ``--port`` override it.
    """
    from urllib.parse import urlsplit

    try:
        import uvicorn
    except ImportError:
        typer.echo(
            "uvicorn not installed. Run: uv sync --extra server", err=True
        )
        raise typer.Exit(2) from None
    from forge_lens.api.app import create_app

    cfg = _config(db)
    try:
        # a schemeless "localhost:7777" would parse "localhost" as the
        # *scheme* — prefix // so it reads as host:port authority
        ep = urlsplit(
            cfg.endpoint if "://" in cfg.endpoint else f"//{cfg.endpoint}"
        )
        ep_port = ep.port  # raises ValueError on a malformed port
    except ValueError as exc:
        typer.echo(
            f"serve failed: invalid endpoint {cfg.endpoint!r}: {exc}", err=True
        )
        raise typer.Exit(2) from None
    host = host or ep.hostname or "127.0.0.1"
    port = port or ep_port or 7777
    application = create_app(config=cfg)
    _warn_if_exposed(host, cfg)
    typer.echo(f"Forge Lens v{__version__} listening on http://{host}:{port}")
    typer.echo("  OTLP endpoint:   POST /v1/otlp/traces")
    typer.echo("  Native endpoint: POST /v1/traces")
    typer.echo(f"  DB: {cfg.resolved_db_path}")
    uvicorn.run(application, host=host, port=port, log_level="info")


@app.command()
def ingest(
    file: Annotated[
        Path,
        typer.Argument(
            help="JSON file: native TraceIngest or OTLP ExportTraceServiceRequest."
        ),
    ],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    fmt: Annotated[str, typer.Option("--format", "-f", help="native|otlp|auto")] = "auto",
    project: Annotated[str | None, typer.Option("--project")] = None,
) -> None:
    """Ingest traces from a JSON file into the local store."""
    from forge_lens.contracts.provenance import Provenance
    from forge_lens.contracts.v1.trace import TraceIngest
    from forge_lens.ingest.pipeline import IngestPipeline
    from forge_lens.otlp.normalize import normalize_otlp_request

    if fmt not in {"native", "otlp", "auto"}:
        typer.echo(
            f"ingest failed: unknown --format {fmt!r} (native|otlp|auto)", err=True
        )
        raise typer.Exit(2)

    cfg = _config(db)
    if project:
        cfg = LensConfig(**{**cfg.__dict__, "project_id": project})

    store = _store(cfg)
    pipeline = IngestPipeline(store, cfg)
    quarantine: list[str] = []
    try:
        payload = json.loads(file.read_text(encoding="utf-8"))
        if fmt == "otlp" or (fmt == "auto" and "resourceSpans" in payload):
            # file import — weaker provenance than a live OTLP push
            traces = normalize_otlp_request(
                payload,
                project_id=cfg.project_id,
                provenance=Provenance.IMPORTED,
                errors=quarantine,
            )
        else:
            envelope = TraceIngest.model_validate(payload)
            # --project is an explicit override; otherwise the envelope
            # fills only traces that didn't name a project of their own.
            traces = envelope.traces
            for t in traces:
                if project is not None:
                    t.project_id = project
                elif t.project_id == "default" and envelope.project_id != "default":
                    t.project_id = envelope.project_id
        result = pipeline.ingest(traces)
    except Exception as exc:
        typer.echo(f"ingest failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        store.close()
    typer.echo(
        f"ingested {result.accepted_spans} spans "
        f"({result.updated_spans} updated, {result.duplicate_spans} duplicates) "
        f"across {len(result.traces)} trace(s)"
    )
    for warning in [*result.warnings, *quarantine]:
        typer.echo(f"  warning: {warning}")


@traces_app.command("list")
def traces_list(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    status: Annotated[str | None, typer.Option("--status")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List stored traces (most recent first)."""
    cfg = _config(db)
    store = _store(cfg)
    try:
        items = store.list_traces(
            project_id=project or cfg.project_id, status=status, limit=limit
        )
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        typer.echo("no traces stored")
        return
    for item in items:
        started = (item["started_at"] or "?")[:19]
        typer.echo(
            f"{item['trace_id']}  {started}  {item['status']:<6} "
            f"spans={item['span_count']:<4} errors={item['error_count']}"
        )


@traces_app.command("show")
def traces_show(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a trace: span tree, durations, errors, token usage."""
    cfg = _config(db)
    store = _store(cfg)
    try:
        trace = store.get_trace(trace_id, project_id=project or cfg.project_id)
    finally:
        store.close()
    if trace is None:
        typer.echo(f"trace {trace_id} not found", err=True)
        raise typer.Exit(1)
    if json_out:
        typer.echo(trace.model_dump_json(indent=2))
        return

    typer.echo(f"trace {trace.trace_id}  status={trace.status.code.value}")
    typer.echo(f"  source: {trace.source.system} ({trace.source.sdk_name or 'unknown sdk'})")
    typer.echo(f"  spans:  {len(trace.spans)}")
    by_parent: dict[str | None, list[Span]] = {}
    for s in trace.spans:
        by_parent.setdefault(s.parent_span_id, []).append(s)
    for children in by_parent.values():
        children.sort(key=lambda s: s.start_time)

    def emit(s: Span, depth: int) -> None:
        dur = f"{s.duration_ms:.1f}ms" if s.duration_ms is not None else "running"
        mark = "!" if s.status.code.value == "error" else " "
        typer.echo(f"  {'  ' * depth}{mark}{s.name} [{s.kind.value}] {dur}")
        for flag in s.integrity_flags:
            typer.echo(f"  {'  ' * depth}  ~ flag: {flag.value}")
        usage = s.attributes.get("forge_lens.token_usage")
        if isinstance(usage, dict):
            parts = [
                f"{k}={v}"
                for k, v in usage.items()
                if k.endswith("tokens") and v is not None
            ]
            if parts:
                typer.echo(f"  {'  ' * depth}  ~ tokens: {', '.join(parts)}")

    # iterative DFS — deep trees must not hit the recursion limit
    rendered: set[str] = set()
    span_ids = {s.span_id for s in trace.spans}
    seeds = [
        *by_parent.get(None, []),
        *(s for s in trace.spans if s.parent_span_id and s.parent_span_id not in span_ids),
    ]
    unrendered = {s.span_id: s for s in trace.spans}
    stack: list[tuple[Span, int]] = [(s, 0) for s in reversed(seeds)]
    while stack or unrendered:
        if not stack:
            # span trapped in a parent cycle — start a fresh tree at depth 0
            stack.append((next(iter(unrendered.values())), 0))
        node, depth = stack.pop()
        if node.span_id in rendered:
            continue
        rendered.add(node.span_id)
        unrendered.pop(node.span_id, None)
        emit(node, depth)
        for child in reversed(by_parent.get(node.span_id, [])):
            stack.append((child, depth + 1))


@app.command()
def doctor(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path(),
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Check the local Forge Lens installation and store health."""
    base = load_config(project_dir=path.resolve())
    cfg = LensConfig(**{**base.__dict__, "db_path": db}) if db is not None else base
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        typer.echo(f"[{'ok' if passed else 'FAIL'}] {name} {detail}")

    check("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    data_dir = cfg.data_dir
    check("data dir exists", data_dir.exists(), str(data_dir))
    db_path = cfg.resolved_db_path
    if db_path.exists():
        try:
            store = _store(cfg)
            stats = store.stats()
            store.close()
            check("sqlite store readable", True, f"schema={stats['schema_version']}")
            typer.echo(f"       traces={stats['traces']} spans={stats['spans']} "
                       f"db={stats['db_size_bytes']}B")
        except Exception as exc:
            check("sqlite store readable", False, str(exc))
    else:
        typer.echo(f"[info] db not created yet: {db_path}")
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
        check("server extras installed", True)
    except ImportError:
        typer.echo("[info] server extras missing — `uv sync --extra server` to enable `serve`")
    try:
        from forge_lens.sdk import LensTracer  # noqa: F401
        check("sdk importable", True)
    except Exception as exc:
        check("sdk importable", False, str(exc))
    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
