"""``forge-lens`` CLI — init, serve, ingest, inspect, doctor.

Local-first: every command works offline against the local store.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
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
keys_app = typer.Typer(help="Manage API keys (Cycle 08).", no_args_is_help=True)
app.add_typer(keys_app, name="keys")


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
    elif not cfg.auth_reads:
        typer.echo(
            f"warning: binding {host} — api_key protects ingest only; "
            "GET /v1/traces, /v1/health and /v1/metrics stay unauthenticated "
            "by design (local-first reads). Set auth_reads=true to guard them.",
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
    ssl_certfile: Annotated[
        Path | None, typer.Option("--ssl-certfile", help="TLS certificate (remote mode)")
    ] = None,
    ssl_keyfile: Annotated[
        Path | None, typer.Option("--ssl-keyfile", help="TLS private key (remote mode)")
    ] = None,
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
    # `or` would eat an explicit --port 0 (ephemeral bind) — None-checks
    # keep 0 meaningful at both levels
    port = port if port is not None else (ep_port if ep_port is not None else 7777)
    application = create_app(config=cfg)
    _warn_if_exposed(host, cfg)
    shown = port if port != 0 else "ephemeral"  # uvicorn logs the real bound port
    scheme = "https" if ssl_certfile else "http"
    typer.echo(f"Forge Lens v{__version__} listening on {scheme}://{host}:{shown}")
    typer.echo("  OTLP endpoint:   POST /v1/otlp/traces")
    typer.echo("  Native endpoint: POST /v1/traces")
    typer.echo(f"  DB: {cfg.resolved_db_path}")
    uvicorn.run(
        application,
        host=host,
        port=port,
        log_level="info",
        ssl_certfile=str(ssl_certfile) if ssl_certfile else None,
        ssl_keyfile=str(ssl_keyfile) if ssl_keyfile else None,
    )


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
        # same bound as the API — stat first (fast reject), then read at
        # most max+1 bytes: read_bytes() on a giant file would OOM before
        # payload_bytes is ever checked
        max_bytes = cfg.max_payload_bytes
        size = file.stat().st_size
        if size > max_bytes:
            raise ValueError(
                f"file {file} ({size}B) exceeds "
                f"max_payload_bytes ({max_bytes}B)"
            )
        with file.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError(
                f"file {file} exceeds max_payload_bytes ({max_bytes}B)"
            )
        payload = json.loads(raw)
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
                # parity with the API path: the envelope's declared
                # sender fills traces that didn't name a producer
                if envelope.source is not None and t.source.system == "unknown":
                    t.source = envelope.source
        # byte bound applies on the CLI path too — same limit the API enforces
        result = pipeline.ingest(traces, payload_bytes=len(raw))
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
    session: Annotated[
        str | None, typer.Option("--session", help="exact session_id")
    ] = None,
    tool: Annotated[
        str | None,
        typer.Option("--tool", help="substring match on span tool.name"),
    ] = None,
    q: Annotated[
        str | None,
        typer.Option("--q", help="substring match on trace_id or span name"),
    ] = None,
    since: Annotated[
        str | None, typer.Option("--since", help="ISO-8601 started_at lower bound")
    ] = None,
    until: Annotated[
        str | None, typer.Option("--until", help="ISO-8601 started_at upper bound")
    ] = None,
    min_duration_ms: Annotated[
        float | None, typer.Option("--min-duration-ms")
    ] = None,
    max_duration_ms: Annotated[
        float | None, typer.Option("--max-duration-ms")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List stored traces (most recent first)."""
    import math

    from forge_lens.core.errors import ValidationError
    from forge_lens.core.time import ensure_aware, to_unix_nano

    cfg = _config(db)

    def _iso_ns(value: str | None, opt: str) -> int | None:
        if value is None:
            return None
        try:
            return to_unix_nano(ensure_aware(datetime.fromisoformat(value)))
        except (ValueError, ValidationError) as exc:
            typer.echo(f"invalid {opt} {value!r}: {exc}", err=True)
            raise typer.Exit(2) from None

    def _dur_ns(value: float | None, opt: str) -> int | None:
        if value is None:
            return None
        if not math.isfinite(value) or value < 0:
            typer.echo(
                f"invalid {opt} {value!r}: non-negative finite ms expected",
                err=True,
            )
            raise typer.Exit(2)
        return int(value * 1_000_000)

    store = _store(cfg)
    try:
        items = store.list_traces(
            project_id=project or cfg.project_id,
            session_id=session,
            status=status,
            since_ns=_iso_ns(since, "--since"),
            until_ns=_iso_ns(until, "--until"),
            min_duration_ns=_dur_ns(min_duration_ms, "--min-duration-ms"),
            max_duration_ns=_dur_ns(max_duration_ms, "--max-duration-ms"),
            q=q,
            tool=tool,
            limit=limit,
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
def graph(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execution graph for a trace: critical path + bottleneck ranking."""
    from forge_lens.graph import build_trace_graph, critical_path, self_time_ranking

    cfg = _config(db)
    store = _store(cfg)
    try:
        trace = store.get_trace(trace_id, project_id=project or cfg.project_id)
    finally:
        store.close()
    if trace is None:
        typer.echo(f"trace {trace_id} not found", err=True)
        raise typer.Exit(1)
    g = build_trace_graph(trace)
    if json_out:
        typer.echo(g.model_dump_json(indent=2))
        return
    typer.echo(
        f"graph {g.subject_id}: {g.stats['span_count']} nodes, "
        f"{g.stats['edge_count']} edges, depth={g.stats['max_depth']}"
    )
    if g.stats["dangling_edges"]:
        typer.echo(f"  ~ {g.stats['dangling_edges']} dangling link edge(s)")
    cp = critical_path(g)
    if cp["path"]:
        names = {n.id: n.name for n in g.nodes}
        typer.echo(f"critical path ({cp['total_ms']}ms):")
        for node_id in cp["path"]:  # type: ignore[attr-defined]
            typer.echo(f"  → {names.get(node_id, node_id)}")
    typer.echo("top self-time:")
    for row in self_time_ranking(g):
        typer.echo(f"  {row['self_ms']:>10}ms  {row['name']} [{row['kind']}]")


@app.command()
def cost(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Token + cost report for a trace (derived estimate + waste findings)."""
    from forge_lens.intel import PricingRegistry, detect_waste, trace_cost

    cfg = _config(db)
    store = _store(cfg)
    try:
        trace = store.get_trace(trace_id, project_id=project or cfg.project_id)
    finally:
        store.close()
    if trace is None:
        typer.echo(f"trace {trace_id} not found", err=True)
        raise typer.Exit(1)
    try:
        registry = (
            PricingRegistry.from_file(cfg.pricing_file)
            if cfg.pricing_file
            else PricingRegistry()
        )
    except Exception as exc:
        typer.echo(f"cost failed: invalid pricing_file: {exc}", err=True)
        raise typer.Exit(2) from None
    report = trace_cost(trace, registry)
    waste = detect_waste(trace, registry)
    if json_out:
        typer.echo(
            report.model_dump_json(indent=2)
            if not waste
            else json.dumps(
                {
                    **report.model_dump(mode="json"),
                    "waste": [w.model_dump(mode="json") for w in waste],
                },
                indent=2,
            )
        )
        return
    typer.echo(f"trace {report.trace_id}  pricing={registry.source}")
    typer.echo(
        f"  tokens: in={report.total_input_tokens} "
        f"out={report.total_output_tokens} total={report.total_tokens}"
    )
    typer.echo(f"  estimated: ${report.total_estimated_usd:.6f}")
    for model, b in report.by_model.items():
        typer.echo(
            f"    {model}: in={b['input_tokens']} out={b['output_tokens']} "
            f"${b['estimated_usd']:.6f}"
        )
    for m in report.unpriced_models:
        typer.echo(f"  ~ unpriced model: {m}")
    for w in waste:
        typer.echo(f"  ~ {w.kind}: {w.detail}")


@app.command()
def incident(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Incident bundle for a trace: failure class + evidence + timeline."""
    from forge_lens.intel import PricingRegistry
    from forge_lens.investigate import incident_bundle

    cfg = _config(db)
    store = _store(cfg)
    try:
        trace = store.get_trace(trace_id, project_id=project or cfg.project_id)
    finally:
        store.close()
    if trace is None:
        typer.echo(f"trace {trace_id} not found", err=True)
        raise typer.Exit(1)
    try:
        registry = (
            PricingRegistry.from_file(cfg.pricing_file)
            if cfg.pricing_file
            else PricingRegistry()
        )
    except Exception as exc:
        typer.echo(f"incident failed: invalid pricing_file: {exc}", err=True)
        raise typer.Exit(2) from None
    bundle = incident_bundle(trace, registry)
    if json_out:
        typer.echo(json.dumps(bundle, indent=2))
        return
    f = bundle["failure"]
    typer.echo(
        f"trace {bundle['trace_id']}  status={bundle['status']}  "
        f"failed={f['failed']}"
    )
    if f["failed"]:
        typer.echo(f"  category: {f['category']} ({f['confidence']})")
        typer.echo(f"  detail:   {f['detail']}")
        for sid in f["evidence"].get("span_ids", []):
            typer.echo(f"  evidence: span {sid}")
    cp = bundle["graph"]["critical_path"]
    if cp["path"]:
        typer.echo(f"  critical path: {len(cp['path'])} spans, {cp['total_ms']}ms")
    typer.echo(
        f"  replay: {len(bundle['replay']['entries'])} timeline entries, "
        f"{bundle['replay']['total_ms']}ms"
    )
    typer.echo(f"  est. cost: ${bundle['cost']['total_estimated_usd']:.6f}")


@app.command(name="eval")
def eval_cmd(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    budget_ms: Annotated[float | None, typer.Option("--budget-ms")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the deterministic evaluator battery on a stored trace."""
    from forge_lens.eval import run_evaluators

    cfg = _config(db)
    store = _store(cfg)
    try:
        trace = store.get_trace(trace_id, project_id=project or cfg.project_id)
    finally:
        store.close()
    if trace is None:
        typer.echo(f"trace {trace_id} not found", err=True)
        raise typer.Exit(1)
    specs: list[dict[str, object]] = [
        {"name": "error_free"},
        {"name": "tool_success"},
    ]
    if budget_ms is not None:
        specs.append({"name": "latency_budget", "params": {"budget_ms": budget_ms}})
    results = run_evaluators(trace, specs)
    if json_out:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in results], indent=2))
        return
    for r in results:
        score = "n/a" if r.score is None else f"{r.score:g}"
        typer.echo(f"  {r.evaluator}/{r.metric}: {score}")
        if r.score is None and r.details.get("reason"):
            typer.echo(f"    ~ {r.details['reason']}")


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
            # read-only probe — a health check must not create tables,
            # write schema_version, or set journal modes
            store = SQLiteTraceStore(db_path, readonly=True)
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
    if cfg.pricing_file:
        try:
            from forge_lens.intel.pricing import PricingRegistry

            PricingRegistry.from_file(cfg.pricing_file)
            check("pricing file parseable", True, str(cfg.pricing_file))
        except Exception as exc:
            check("pricing file parseable", False, str(exc))
    raise typer.Exit(0 if ok else 1)


@keys_app.command("create")
def keys_create(
    name: Annotated[str, typer.Option("--name")],
    role: Annotated[str, typer.Option("--role", help="reader|writer|admin")] = "writer",
    project: Annotated[
        list[str] | None,
        typer.Option("--project", help="tenant scope; repeatable; omit for unscoped"),
    ] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Issue an API key. The secret prints once — only its hash is stored."""
    from forge_lens.api.clock import wall_clock_ns
    from forge_lens.security.keys import ROLES, generate_key

    if role not in ROLES:
        typer.echo(f"invalid role {role!r} — one of {ROLES}", err=True)
        raise typer.Exit(2)
    store = _store(_config(db))
    try:
        key_id, secret, hashed = generate_key()
        store.create_api_key(
            key_id, name=name, key_hash=hashed, role=role,
            projects=project, now_ns=wall_clock_ns(),
        )
    finally:
        store.close()
    typer.echo(f"key_id:  {key_id}")
    typer.echo(f"secret:  {secret}   # shown once — store keeps only its hash")
    typer.echo(f"role:    {role}")
    typer.echo(f"projects: {project or ['*unscoped*']}")


@keys_app.command("list")
def keys_list(db: Annotated[Path | None, typer.Option("--db")] = None) -> None:
    store = _store(_config(db))
    try:
        for k in store.list_api_keys():
            state = "revoked" if k["revoked_at"] else "active"
            typer.echo(
                f"{k['key_id']}  {k['name']}  role={k['role']}  "
                f"projects={k['projects'] or ['*']}  {state}"
            )
    finally:
        store.close()


@keys_app.command("revoke")
def keys_revoke(
    key_id: str, db: Annotated[Path | None, typer.Option("--db")] = None
) -> None:
    from forge_lens.api.clock import wall_clock_ns

    store = _store(_config(db))
    try:
        if not store.revoke_api_key(key_id, now_ns=wall_clock_ns()):
            typer.echo(f"key {key_id!r} not found or already revoked", err=True)
            raise typer.Exit(1)
    finally:
        store.close()
    typer.echo(f"revoked {key_id}")


@app.command()
def prune(
    before: Annotated[
        str | None, typer.Option("--before", help="ISO datetime cutoff")
    ] = None,
    days: Annotated[
        int | None, typer.Option("--days", help="delete traces older than N days")
    ] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="confirm deletion")] = False,
) -> None:
    """Retention enforcement — delete traces older than a cutoff.

    Destructive: requires --yes or an interactive confirmation."""
    from forge_lens.api.clock import wall_clock_ns
    from forge_lens.core.time import ensure_aware, to_unix_nano

    cfg = _config(db)
    if before is not None and days is not None:
        typer.echo("--before and --days are exclusive", err=True)
        raise typer.Exit(2)
    if before is not None:
        try:
            cutoff = to_unix_nano(ensure_aware(datetime.fromisoformat(before)))
        except ValueError:
            typer.echo(f"invalid --before {before!r}", err=True)
            raise typer.Exit(2) from None
    elif days is not None:
        if days < 0:
            typer.echo("--days must be non-negative", err=True)
            raise typer.Exit(2)
        cutoff = wall_clock_ns() - days * 86_400_000_000_000
    elif cfg.retention_days > 0:
        cutoff = wall_clock_ns() - cfg.retention_days * 86_400_000_000_000
    else:
        typer.echo(
            "no cutoff: pass --before, --days, or set retention_days in config",
            err=True,
        )
        raise typer.Exit(2)
    store = _store(cfg)
    try:
        if not yes:
            typer.confirm(
                f"delete all traces older than the cutoff in "
                f"project {project or '*'}? This cannot be undone.",
                abort=True,
            )
        deleted = store.prune_traces(before_ns=cutoff, project_id=project)
        store.record_audit(
            actor="cli", action="traces.prune",
            resource=project or "*", outcome="ok",
            detail={"before_ns": cutoff, **deleted}, ts_ns=wall_clock_ns(),
        )
    finally:
        store.close()
    typer.echo(f"pruned {deleted['traces']} trace(s)")


@app.command()
def archive(
    before: Annotated[str, typer.Option(
        "--before", help="ISO cutoff — traces ended before this are exported")],
    out: Annotated[Path, typer.Option("--out", help="NDJSON archive path")],
    project: Annotated[str | None, typer.Option("--project")] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
    delete: Annotated[bool, typer.Option(
        "--delete", help="prune after archiving (needs --yes)")] = False,
    append: Annotated[bool, typer.Option(
        "--append", help="append to an existing archive file")] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y")] = False,
) -> None:
    """Tiered storage: export traces older than a cutoff to NDJSON.

    Archive-first lifecycle: data leaves the hot store only after it
    lands in the archive file — never delete-before-export."""
    import json as _json

    from forge_lens.api.clock import wall_clock_ns
    from forge_lens.core.time import ensure_aware, to_unix_nano

    try:
        cutoff = to_unix_nano(ensure_aware(datetime.fromisoformat(before)))
    except ValueError:
        typer.echo(f"invalid --before {before!r}", err=True)
        raise typer.Exit(2) from None
    if out.exists() and not append:
        typer.echo(
            f"{out} exists — refusing to truncate a prior archive "
            "(pass --append to append)", err=True,
        )
        raise typer.Exit(2)
    cfg = _config(db)
    store = _store(cfg)
    try:
        written_ids: list[tuple[str, str]] = []
        offset = 0
        page = 1000
        with out.open("a" if append else "w", encoding="utf-8") as fh:
            while True:
                rows = store.list_traces(
                    project_id=project, limit=page, offset=offset
                )
                if not rows:
                    break
                for row in rows:
                    end_iso = row.get("ended_at")
                    if end_iso is None:
                        continue  # un-ended trace: never archived
                    if to_unix_nano(ensure_aware(
                        datetime.fromisoformat(end_iso)
                    )) >= cutoff:
                        continue
                    tr = store.get_trace(
                        row["trace_id"], project_id=row["project_id"]
                    )
                    if tr is None:
                        continue
                    fh.write(_json.dumps(tr.model_dump(mode="json"),
                                         default=str) + "\n")
                    written_ids.append(
                        (str(row["project_id"]), tr.trace_id)
                    )
                offset += len(rows)
                if len(rows) < page:
                    break
        typer.echo(f"archived {len(written_ids)} trace(s) → {out}")
        if delete:
            if not yes:
                typer.confirm(
                    f"delete the {len(written_ids)} archived trace(s) "
                    "from the hot store?",
                    abort=True,
                )
            by_proj: dict[str, list[str]] = {}
            for _pid, _tid in written_ids:
                by_proj.setdefault(_pid, []).append(_tid)
            deleted = {"traces": sum(
                store.delete_trace_ids(ids, project_id=_pid)["traces"]
                for _pid, ids in sorted(by_proj.items())
            )}
            store.record_audit(
                actor="cli", action="traces.archive_prune",
                resource=str(out), outcome="ok",
                detail={"archived": len(written_ids), **deleted},
                ts_ns=wall_clock_ns(),
            )
            typer.echo(f"pruned {deleted['traces']} trace(s)")
    finally:
        store.close()


@app.command(name="mcp-serve")
def mcp_serve(db: Annotated[Path | None, typer.Option("--db")] = None) -> None:
    """Run the MCP stdio observer server (tools/list, tools/call)."""
    from forge_lens.mcp_server import serve

    cfg = _config(db)
    raise typer.Exit(serve(str(cfg.resolved_db_path)))


@app.command(name="import-claude")
def import_claude(
    path: Annotated[Path, typer.Argument(help="session .jsonl file")],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str, typer.Option("--project")] = "claude-code",
) -> None:
    """Import a Claude Code session JSONL as a log-derived trace."""
    from forge_lens.adapters.claude_code import parse_session

    try:
        trace = parse_session(path, project_id=project)
    except FileNotFoundError:
        typer.echo(f"file not found: {path}", err=True)
        raise typer.Exit(2) from None
    except OSError as exc:
        typer.echo(f"cannot read {path}: {exc}", err=True)
        raise typer.Exit(2) from None
    except ValueError as exc:
        typer.echo(f"invalid session file: {exc}", err=True)
        raise typer.Exit(2) from None
    if trace is None:
        typer.echo("no usable events in file — nothing imported", err=True)
        raise typer.Exit(1)
    from forge_lens.core.errors import ValidationError
    from forge_lens.ingest.pipeline import IngestPipeline

    cfg = _config(db)
    store = _store(cfg)
    try:
        # go through the pipeline, not a bare upsert — _ensure_storable
        # validates encodability/ranges so a hostile JSONL can't reach a
        # SQLite bind (same guarantee ingest has)
        result = IngestPipeline(store, cfg).ingest([trace])
        for w in result.warnings:
            typer.echo(f"warning: {w}", err=True)
    except ValidationError as exc:
        typer.echo(f"import failed: {exc}", err=True)
        raise typer.Exit(2) from None
    finally:
        store.close()
    typer.echo(f"imported {len(trace.spans)} span(s) → {trace.trace_id}")


@app.command(name="install-status")
def install_status(
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Show install receipt / doctor report."""
    import json as _json

    from forge_lens.install.lifecycle import doctor, status

    d = data_dir or Path("./.forge-lens")
    typer.echo(_json.dumps(
        {"status": status(d), "doctor": doctor(d)}, indent=2))


if __name__ == "__main__":
    app()
