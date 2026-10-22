"""``forge-lens`` CLI — init, serve, ingest, inspect, doctor.

Local-first: every command works offline against the local store.
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from forge_lens import __version__
from forge_lens.contracts.v1.trace import Span, Trace
from forge_lens.core.config import (
    DEFAULT_DIR_NAME,
    LensConfig,
    init_project,
    load_config,
)
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
datasets_app = typer.Typer(help="Inspect eval datasets.", no_args_is_help=True)
app.add_typer(datasets_app, name="datasets")
experiments_app = typer.Typer(
    help="Persisted A/B eval experiments (Cycle 13).", no_args_is_help=True
)
app.add_typer(experiments_app, name="experiments")
alerts_app = typer.Typer(
    help="Threshold alert rules over store metrics.", no_args_is_help=True
)
app.add_typer(alerts_app, name="alerts")
intel_app = typer.Typer(
    help="Fleet-level intelligence over stored traces.", no_args_is_help=True
)
app.add_typer(intel_app, name="intel")
install_app = typer.Typer(
    help=(
        "Install lifecycle — record an install receipt, inspect status, "
        "health-check, or uninstall tracked files. The portable bootstrap "
        "(install.sh/install.ps1) performs the real install; these verbs "
        "manage the receipt it leaves under <data-dir>/install/."
    ),
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(install_app, name="install")


def _config(db: Path | None = None) -> LensConfig:
    try:
        base = load_config()
    except ValueError as exc:
        # malformed config.toml / FORGE_LENS_* env is operator input —
        # report it like any other invalid flag, never a traceback
        typer.echo(f"invalid configuration: {exc}", err=True)
        raise typer.Exit(2) from None
    if db is not None:
        return LensConfig(**{**base.__dict__, "db_path": db})
    return base


def _store(cfg: LensConfig) -> SQLiteTraceStore:
    try:
        return SQLiteTraceStore(cfg.resolved_db_path)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        # a --db pointing at a directory, a non-sqlite file, an
        # unwritable parent or a symlink loop (Path.resolve raises
        # RuntimeError there, not OSError) is invalid input — clean
        # message + exit 2, same contract mcp-serve reports for an
        # unopenable store
        typer.echo(
            f"cannot open store at {cfg.resolved_db_path}: {exc}", err=True
        )
        raise typer.Exit(2) from None


def _get_trace_or_exit(
    trace_id: str, cfg: LensConfig, project: str | None
) -> Trace:
    """Store lookup shared by the per-trace analysis commands — a
    missing trace is a clean 'not found', never a traceback."""
    store = _store(cfg)
    try:
        trace = store.get_trace(trace_id, project_id=project or cfg.project_id)
    finally:
        store.close()
    if trace is None:
        typer.echo(f"trace {trace_id} not found", err=True)
        raise typer.Exit(2)
    return trace


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
    if port is not None and not 0 <= port <= 65535:
        # invalid flag would otherwise crash inside uvicorn at bind time
        typer.echo(
            f"invalid --port {port}: range is 0-65535 (0 = ephemeral)",
            err=True,
        )
        raise typer.Exit(2)
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
    model: Annotated[
        str | None,
        typer.Option(
            "--model", help="substring on gen_ai.request/response.model"),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option(
            "--agent",
            help="substring on gen_ai.agent.name or producer identity"),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option("--host", help="substring on source.host / host.name"),
    ] = None,
    min_tokens: Annotated[
        int | None,
        typer.Option(
            "--min-tokens",
            help="summed gen_ai.usage.total_tokens ≥ value"),
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
    offset: Annotated[
        int, typer.Option("--offset", help="rows to skip (pagination)")
    ] = 0,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List stored traces (most recent first)."""
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
        ns = int(value * 1_000_000)
        if ns > 2**63 - 1:  # finite floats still overflow the sqlite bind
            typer.echo(f"invalid {opt} {value!r}: out of int64 range",
                       err=True)
            raise typer.Exit(2)
        return ns

    # flag validation precedes the store open — an early Exit here would
    # otherwise leak the connection (and a negative --limit must not
    # reach the store's silent max(0, limit) clamp)
    if min_tokens is not None and (
        min_tokens < 0 or min_tokens > 2**63 - 1  # sqlite int64 bound
    ):
        typer.echo("invalid --min-tokens: integer in 0..2**63-1 expected",
                   err=True)
        raise typer.Exit(2)
    if limit < 0:
        typer.echo("invalid --limit: non-negative integer expected", err=True)
        raise typer.Exit(2)
    if limit > 2**63 - 1:
        typer.echo("invalid --limit: out of int64 range", err=True)
        raise typer.Exit(2)
    if offset < 0:
        typer.echo("invalid --offset: non-negative integer expected", err=True)
        raise typer.Exit(2)
    if offset > 2**63 - 1:
        typer.echo("invalid --offset: out of int64 range", err=True)
        raise typer.Exit(2)
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
            model=model,
            agent=agent,
            host=host,
            min_total_tokens=min_tokens,
            limit=limit,
            offset=offset,
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
    trace = _get_trace_or_exit(trace_id, _config(db), project)
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

    trace = _get_trace_or_exit(trace_id, _config(db), project)
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
def sessions(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Session roll-up: one row per session_id, newest activity first."""
    cfg = _config(db)
    store = _store(cfg)
    try:
        items = store.list_sessions(
            project or cfg.project_id, limit=min(500, max(0, limit))
        )
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        typer.echo("no sessions recorded")
        return
    for s in items:
        last = (s["last_activity_at"] or "?")[:19]
        typer.echo(
            f"{s['session_id']:<28} traces={s['traces']:<5} "
            f"spans={s['spans']:<6} last={last}"
        )


@app.command()
def tools(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Fleet-wide tool roll-up: calls, traces, errors, avg duration per tool."""
    cfg = _config(db)
    store = _store(cfg)
    try:
        items = store.list_tools(
            project or cfg.project_id, limit=min(500, max(0, limit))
        )
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        typer.echo("no tool spans recorded")
        return
    for t in items:
        avg = f"{t['avg_duration_ms']:.0f}ms" if t["avg_duration_ms"] else "?"
        typer.echo(
            f"{t['tool']:<24} calls={t['calls']:<6} "
            f"traces={t['traces']:<5} errors={t['errors']:<4} avg={avg}"
        )


@app.command()
def capabilities(
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Host adapter capability matrix — the same payload as
    ``GET /v1/capabilities`` / ``lens.capabilities`` (local, no store)."""
    from forge_lens.adapters.capabilities import capability_matrix

    matrix = capability_matrix()
    if json_out:
        typer.echo(json.dumps(matrix, indent=2))
        return
    for host, caps in matrix["hosts"].items():
        native = "native tracing" if caps["native_tracing"] else "no native tracing"
        typer.echo(f"{host}  {native}")
        typer.echo(f"  mechanism: {caps['mechanism']}")
        if caps.get("adapter"):
            typer.echo(f"  adapter:   {caps['adapter']}")
        typer.echo(f"  coverage:  {caps['coverage']}")
        typer.echo(f"  limits:    {caps['limits']}")
    typer.echo(f"note: {matrix['note']}")


@app.command()
def stats(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Ops summary: store stats + recent failures — the same payload as
    ``GET /v1/ops/summary`` (bounded 200-trace window)."""
    cfg = _config(db)
    store = _store(cfg)
    try:
        pid = project or cfg.project_id
        rows = store.list_traces(project_id=pid, limit=200)
        out: dict[str, Any] = {
            "project_id": pid,
            "stats": store.stats(project_id=pid),
            "recent_traces": len(rows),
            "recent_failures": sorted(
                (t["trace_id"] for t in rows if t.get("status") == "error"),
                reverse=True,
            )[:50],
            "note": "bounded window (200 traces) — not a fleet-wide total",
        }
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    s = out["stats"]
    typer.echo(
        f"project {out['project_id']}  schema={s['schema_version']}  "
        f"db={s['db_size_bytes']}B"
    )
    typer.echo(
        f"  traces={s['traces']} spans={s['spans']} "
        f"error_spans={s['error_spans']}"
    )
    typer.echo(f"  recent window: {out['recent_traces']} trace(s)")
    failures = out["recent_failures"]
    if failures:
        typer.echo(f"  recent failures ({len(failures)}):")
        for tid in failures:
            typer.echo(f"    {tid}")


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
    trace = _get_trace_or_exit(trace_id, cfg, project)
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
    trace = _get_trace_or_exit(trace_id, cfg, project)
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
    evaluator: Annotated[
        list[str] | None,
        typer.Option(
            "--evaluator",
            help="extra evaluator from the registry; repeatable",
        ),
    ] = None,
    params_json: Annotated[
        str | None,
        typer.Option(
            "--params-json",
            help="JSON object of params shared by every --evaluator spec",
        ),
    ] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run the deterministic evaluator battery on a stored trace."""
    from forge_lens.eval import EVALUATORS, run_evaluators

    params: dict[str, Any] = {}
    if params_json is not None:
        try:
            parsed = json.loads(params_json)
        except (ValueError, RecursionError) as exc:
            typer.echo(f"invalid --params-json: {exc}", err=True)
            raise typer.Exit(2) from None
        if not isinstance(parsed, dict):
            typer.echo(
                "invalid --params-json: a JSON object is required", err=True
            )
            raise typer.Exit(2)
        params = parsed
    unknown = [n for n in (evaluator or []) if n not in EVALUATORS]
    if unknown:
        typer.echo(
            f"unknown evaluator {unknown[0]!r} — one of "
            f"{sorted(EVALUATORS)}",
            err=True,
        )
        raise typer.Exit(2)
    trace = _get_trace_or_exit(trace_id, _config(db), project)
    specs: list[dict[str, object]] = [
        {"name": "error_free"},
        {"name": "tool_success"},
    ]
    if budget_ms is not None:
        specs.append({"name": "latency_budget", "params": {"budget_ms": budget_ms}})
    for name in evaluator or []:
        specs.append({"name": name, "params": dict(params)})
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
def failure(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Deterministic failure classification for a trace (``/v1/traces/{id}/failure``)."""
    from forge_lens.investigate import classify_failure

    trace = _get_trace_or_exit(trace_id, _config(db), project)
    report = classify_failure(trace)
    if json_out:
        typer.echo(report.model_dump_json(indent=2))
        return
    typer.echo(f"trace {report.trace_id}  failed={report.failed}")
    if report.failed:
        typer.echo(f"  category: {report.category.value} ({report.confidence})")
        typer.echo(f"  detail:   {report.detail}")
        for sid in report.evidence.get("span_ids", []):
            typer.echo(f"  evidence: span {sid}")


@app.command()
def loops(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    repeat_threshold: Annotated[
        int, typer.Option("--repeat-threshold", help="min run length flagged")
    ] = 3,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Heuristic loop/no-progress findings for a trace (``/v1/traces/{id}/loops``)."""
    from forge_lens.ops.loops import detect_loops

    trace = _get_trace_or_exit(trace_id, _config(db), project)
    # same bound as the API route's repeat_threshold query param
    out = detect_loops(trace, repeat_threshold=max(1, min(50, repeat_threshold)))
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    findings = out["findings"]
    if not findings:
        typer.echo(f"trace {out['trace_id']}: no loop findings")
        return
    typer.echo(
        f"trace {out['trace_id']}: {len(findings)} finding(s)"
        " (heuristic — repetition is evidence, not a verdict)"
    )
    for f in findings:
        typer.echo(f"  {f['kind']} x{f['count']}: {f['detail']}")
        for sid in f["span_ids"]:
            typer.echo(f"    evidence: span {sid}")


@app.command()
def efficiency(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Per-trace efficiency roll-up — tokens/tools/retries
    (``/v1/traces/{id}/efficiency``)."""
    from forge_lens.intel.efficiency import trace_efficiency

    trace = _get_trace_or_exit(trace_id, _config(db), project)
    out = trace_efficiency(trace)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        f"trace {out['trace_id']}  outcome={out['outcome']}  "
        f"succeeded={out['succeeded']}"
    )
    typer.echo(
        f"  tools={out['tool_calls']} llm={out['llm_calls']} "
        f"errors={out['error_spans']} retries={out['retries_observed']}"
    )
    typer.echo(
        f"  tokens: in={out['input_tokens']} out={out['output_tokens']} "
        f"({out['usage_coverage']})"
    )


@app.command()
def recommendations(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Candidate recommendations with evidence — gains unclaimed
    (``/v1/traces/{id}/recommendations``)."""
    from forge_lens.intel.recommendations import recommend

    trace = _get_trace_or_exit(trace_id, _config(db), project)
    out = recommend(trace)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    recs = out["recommendations"]
    if not recs:
        typer.echo(f"trace {out['trace_id']}: no recommendations")
        return
    typer.echo(
        f"trace {out['trace_id']}: {len(recs)} candidate recommendation(s)"
        " — gains require experimental validation"
    )
    for r in recs:
        typer.echo(f"  {r['kind']}: {r['detail']}")


@app.command()
def correlate(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Temporal co-occurrence signals within one trace — descriptive
    associations, never causal (``/v1/traces/{id}/correlate``)."""
    from forge_lens.intel.correlation import correlate_trace

    trace = _get_trace_or_exit(trace_id, _config(db), project)
    out = correlate_trace(trace)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    cors = out["correlations"]
    if not cors:
        typer.echo(f"trace {out['trace_id']}: no correlations")
        return
    typer.echo(
        f"trace {out['trace_id']}: {len(cors)} temporal association(s)"
        " — correlation is not causation"
    )
    for c in cors:
        typer.echo(
            f"  {c['kind']} (strength={c['strength']}): {c['detail']}")


@app.command()
def security(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """MCP/tool-call security observations for a trace — observer mode,
    evidence only (``/v1/traces/{id}/security``)."""
    from forge_lens.security.mcp_inspect import inspect_trace

    trace = _get_trace_or_exit(trace_id, _config(db), project)
    out = inspect_trace(trace)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    observations = out["observations"]
    if not observations:
        typer.echo(f"trace {out['trace_id']}: no security observations")
        return
    typer.echo(
        f"trace {out['trace_id']}: {len(observations)} observation(s)"
        " (observer mode — evidence only)"
    )
    for o in observations:
        typer.echo(
            f"  [{o['severity']}] {o['kind']} span {o['span_id']}: {o['detail']}"
        )


@app.command()
def replay(
    trace_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Recorded-execution timeline for a trace (``/v1/traces/{id}/replay``)."""
    from forge_lens.investigate import replay_timeline

    trace = _get_trace_or_exit(trace_id, _config(db), project)
    out = replay_timeline(trace)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        f"trace {out['trace_id']}: {out['span_count']} span(s), "
        f"{len(out['entries'])} timeline entries, {out['total_ms']}ms"
    )
    if out["unclosed_spans"]:
        typer.echo(f"  ~ {out['unclosed_spans']} unclosed span(s)")
    for e in out["entries"]:
        detail = ""
        if e["kind"] == "event":
            detail = f" event={e['event']}"
        elif e["kind"] == "span_end":
            detail = f" status={e['status']}"
        typer.echo(f"  +{e['offset_ms']:>9.3f}ms {e['kind']:<10} {e['name']}{detail}")


@app.command()
def doctor(
    path: Annotated[Path, typer.Argument(help="Project directory.")] = Path(),
    db: Annotated[Path | None, typer.Option("--db")] = None,
) -> None:
    """Check the local Forge Lens installation and store health."""
    cfg_error: str | None = None
    try:
        base = load_config(project_dir=path.resolve())
    except ValueError as exc:
        # a malformed config.toml is exactly what doctor exists to
        # surface — report it as a failed check and keep diagnosing
        # with defaults instead of dying on a bare exception
        cfg_error = str(exc)
        base = LensConfig(data_dir=path.resolve() / DEFAULT_DIR_NAME)
    cfg = LensConfig(**{**base.__dict__, "db_path": db}) if db is not None else base
    ok = True

    def check(name: str, passed: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and passed
        typer.echo(f"[{'ok' if passed else 'FAIL'}] {name} {detail}")

    check("python>=3.11", sys.version_info >= (3, 11), sys.version.split()[0])
    check("config parseable", cfg_error is None, cfg_error or "")
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


@datasets_app.command("list")
def datasets_list(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List eval datasets in the store (``GET /v1/datasets``)."""
    cfg = _config(db)
    store = _store(cfg)
    try:
        items = store.list_datasets(project or cfg.project_id)
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        typer.echo("no datasets recorded")
        return
    for d in items:
        created = (d["created_at"] or "?")[:19]
        typer.echo(
            f"{d['dataset_id']:<24} {d['name']:<24} "
            f"task={d['task'] or '-'}  created={created}"
        )


@datasets_app.command("show")
def datasets_show(
    dataset_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Dataset metadata + its append-only versions
    (``GET /v1/datasets/{id}`` + ``/versions``)."""
    cfg = _config(db)
    store = _store(cfg)
    try:
        pid = project or cfg.project_id
        ds = store.get_dataset(dataset_id, pid)
        if ds is None:
            typer.echo(f"dataset {dataset_id!r} not found", err=True)
            raise typer.Exit(2)
        versions = store.list_dataset_versions(dataset_id, pid)
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps({**ds, "versions": versions}, indent=2))
        return
    typer.echo(f"dataset {ds['dataset_id']}  project={ds['project_id']}")
    typer.echo(f"  name:    {ds['name']}")
    if ds["task"]:
        typer.echo(f"  task:    {ds['task']}")
    if ds["created_at"]:
        typer.echo(f"  created: {ds['created_at']}")
    if ds["metadata"]:
        typer.echo(f"  metadata: {json.dumps(ds['metadata'], sort_keys=True)}")
    if not versions:
        typer.echo("  no versions")
    for v in versions:
        line = f"  version {v['version']}: {v['case_count']} case(s)"
        if v["description"]:
            line += f" — {v['description']}"
        if v["created_at"]:
            line += f"  created={v['created_at'][:19]}"
        typer.echo(line)


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
    from forge_lens.core.errors import ValidationError
    from forge_lens.core.time import ensure_aware, to_unix_nano

    cfg = _config(db)
    if before is not None and days is not None:
        typer.echo("--before and --days are exclusive", err=True)
        raise typer.Exit(2)
    if before is not None:
        try:
            cutoff = to_unix_nano(ensure_aware(datetime.fromisoformat(before)))
        except (ValueError, ValidationError):
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
    from forge_lens.core.errors import ValidationError
    from forge_lens.core.time import ensure_aware, to_unix_nano

    try:
        cutoff = to_unix_nano(ensure_aware(datetime.fromisoformat(before)))
    except (ValueError, ValidationError):
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


@app.command(name="import-otel")
def import_otel(
    file: Annotated[
        Path,
        typer.Argument(help="OTLP/HTTP JSON ExportTraceServiceRequest dump."),
    ],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
) -> None:
    """Import an OTLP/HTTP-JSON trace dump into the local store.

    Accepts an ``ExportTraceServiceRequest`` as a JSON object only —
    protobuf payloads are not decodable here (same contract as
    ``POST /v1/otlp/traces``). Malformed spans are quarantined and
    surfaced as warnings instead of rejecting the batch.
    """
    from pydantic import ValidationError as PydanticValidationError

    from forge_lens.contracts.provenance import Provenance
    from forge_lens.core.errors import (
        PayloadTooLargeError,
        RateLimitedError,
        ValidationError,
    )
    from forge_lens.ingest.pipeline import IngestPipeline
    from forge_lens.otlp.normalize import normalize_otlp_request

    cfg = _config(db)
    if project:
        cfg = LensConfig(**{**cfg.__dict__, "project_id": project})
    # same bound as the API's _read_capped_body — stat first (fast
    # reject), then read at most max+1 bytes: read_bytes() on a giant
    # file would OOM before payload_bytes is ever checked
    max_bytes = cfg.max_payload_bytes
    try:
        size = file.stat().st_size
        if size > max_bytes:
            typer.echo(
                f"import-otel failed: file {file} ({size}B) exceeds "
                f"max_payload_bytes ({max_bytes}B)",
                err=True,
            )
            raise typer.Exit(2)
        with file.open("rb") as fh:
            raw = fh.read(max_bytes + 1)
        if len(raw) > max_bytes:
            typer.echo(
                f"import-otel failed: file {file} exceeds "
                f"max_payload_bytes ({max_bytes}B)",
                err=True,
            )
            raise typer.Exit(2)
    except FileNotFoundError:
        typer.echo(f"file not found: {file}", err=True)
        raise typer.Exit(2) from None
    except OSError as exc:
        typer.echo(f"cannot read {file}: {exc}", err=True)
        raise typer.Exit(2) from None
    try:
        payload = json.loads(raw)
    except Exception as exc:
        typer.echo(f"invalid JSON in {file}: {exc}", err=True)
        raise typer.Exit(2) from None
    if not isinstance(payload, dict):
        typer.echo(
            "import-otel failed: OTLP file must be a JSON object "
            "(ExportTraceServiceRequest)",
            err=True,
        )
        raise typer.Exit(2)
    quarantine: list[str] = []
    try:
        # file import — weaker provenance than a live OTLP push
        traces = normalize_otlp_request(
            payload,
            project_id=cfg.project_id,
            provenance=Provenance.IMPORTED,
            errors=quarantine,
        )
    except (ValidationError, PydanticValidationError) as exc:
        typer.echo(f"import-otel failed: {exc}", err=True)
        raise typer.Exit(2) from None
    store = _store(cfg)
    try:
        # go through the pipeline, not a bare upsert — _ensure_storable
        # validates encodability/ranges so a hostile dump can't reach a
        # SQLite bind (same guarantee ingest has)
        result = IngestPipeline(store, cfg).ingest(
            traces, payload_bytes=len(raw)
        )
    except (PayloadTooLargeError, RateLimitedError, ValidationError) as exc:
        typer.echo(f"import-otel failed: {exc}", err=True)
        raise typer.Exit(1) from None
    finally:
        store.close()
    typer.echo(
        f"imported {len(result.traces)} trace(s), "
        f"{result.accepted_spans} span(s) "
        f"({result.updated_spans} updated, {result.duplicate_spans} duplicates)"
    )
    for warning in [*result.warnings, *quarantine]:
        typer.echo(f"  warning: {warning}")


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


def _install_data_dir(data_dir: Path | None) -> Path:
    return data_dir or Path("./.forge-lens")


@install_app.callback()
def install_apply(
    ctx: typer.Context,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    profile: Annotated[str, typer.Option(
        "--profile", help="minimal|recommended|full.")] = "recommended",
    source: Annotated[str, typer.Option(
        "--source", help="pypi|wheel|git|zip|local|gitless|offline.")] = "local",
    file: Annotated[list[str] | None, typer.Option(
        "--file", help="Managed file (relative to data-dir); repeatable.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Record an install receipt (no subcommand = apply)."""
    if ctx.invoked_subcommand is not None:
        return
    import time

    from forge_lens.install.lifecycle import install

    out = install(
        _install_data_dir(data_dir),
        profile=profile, source=source, files=list(file or []),
        ts_ns=time.time_ns(), dry_run=dry_run,
    )
    typer.echo(json.dumps(out, indent=2))
    if not out.get("ok"):
        raise typer.Exit(1)


@install_app.command("status")
def install_status_cmd(
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Show the install receipt, if any."""
    from forge_lens.install.lifecycle import status

    typer.echo(json.dumps(status(_install_data_dir(data_dir)), indent=2))


@install_app.command("doctor")
def install_doctor_cmd(
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Health-check the install receipt and data dir."""
    from forge_lens.install.lifecycle import doctor

    out = doctor(_install_data_dir(data_dir))
    typer.echo(json.dumps(out, indent=2))
    if not out.get("ok"):
        raise typer.Exit(1)


@install_app.command("uninstall")
def install_uninstall_cmd(
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
) -> None:
    """Remove tracked files and write an uninstall receipt."""
    import time

    from forge_lens.install.lifecycle import uninstall

    out = uninstall(_install_data_dir(data_dir), ts_ns=time.time_ns())
    typer.echo(json.dumps(out, indent=2))
    if not out.get("ok"):
        raise typer.Exit(1)


# -- Cycle 07/12/13 surfaces: analyze, audit, experiments, alerts, intel ----


def _pricing_registry(cfg: LensConfig) -> Any:
    """Registry resolution shared with the API (``create_app``) and the
    cost/incident commands — an invalid ``pricing_file`` is a usage
    error reported at the boundary, never a traceback."""
    from forge_lens.intel.pricing import PricingRegistry

    try:
        return (
            PricingRegistry.from_file(cfg.pricing_file)
            if cfg.pricing_file
            else PricingRegistry()
        )
    except Exception as exc:
        typer.echo(f"invalid pricing_file: {exc}", err=True)
        raise typer.Exit(2) from None


def _json_option(raw: str, opt: str) -> Any:
    """Parse a ``--*-json`` CLI option — malformed JSON is invalid
    input (exit 2), never a traceback."""
    try:
        return json.loads(raw)
    except (ValueError, RecursionError) as exc:
        typer.echo(f"invalid {opt}: {exc}", err=True)
        raise typer.Exit(2) from None


def _check_i64_opt(value: int, opt: str) -> int:
    """Non-negative int inside sqlite int64 — the CLI-side counterpart
    of the API's ``_query_i64``."""
    if value < 0:
        typer.echo(f"invalid {opt}: non-negative integer expected", err=True)
        raise typer.Exit(2)
    if value > 2**63 - 1:
        typer.echo(f"invalid {opt}: out of int64 range", err=True)
        raise typer.Exit(2)
    return value


_SPECIALIST_BLURB: dict[str, str] = {
    "context_efficiency_analyst":
        "cumulative input-token growth across LLM spans",
    "cost_analyst":
        "token + USD cost report via the pricing registry",
    "evaluation_analyst":
        "error_free + tool_success evaluator results",
    "evidence_validator":
        "spans missing name or start_time (evidence gaps)",
    "failure_investigator":
        "failure classification + hypotheses + eval suggestions",
    "graph_analyst":
        "critical path over the execution graph",
    "loop_detection_specialist":
        "heuristic loop / no-progress findings",
    "report_specialist":
        "report roll-up over the stored evidence",
    "security_observation_analyst":
        "MCP/tool-call security observations (observer mode)",
    "tool_execution_analyst":
        "TOOL span count + failed span ids",
    "trace_investigator":
        "span count + status summary",
}


@app.command()
def analyze(
    specialist: Annotated[
        str | None,
        typer.Argument(help="registered specialist name — see --list"),
    ] = None,
    trace_id: Annotated[str | None, typer.Argument()] = None,
    list_specs: Annotated[
        bool, typer.Option("--list", help="list specialists and exit")
    ] = False,
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Run one registered specialist over a stored trace
    (``POST /v1/analyze/{specialist}/{trace_id}``)."""
    from forge_lens.ops.agents import SPECIALISTS, run_specialist

    if list_specs:
        rows = [
            {"name": n, "description": _SPECIALIST_BLURB.get(n, "")}
            for n in SPECIALISTS
        ]
        if json_out:
            typer.echo(json.dumps(rows, indent=2))
            return
        for r in rows:
            typer.echo(f"{r['name']:<32} {r['description']}")
        return
    if specialist is None or trace_id is None:
        typer.echo(
            "usage: forge-lens analyze <specialist> <trace_id> "
            "(or --list)", err=True,
        )
        raise typer.Exit(2)
    if specialist not in SPECIALISTS:
        typer.echo(
            f"unknown specialist {specialist!r} — one of "
            f"{list(SPECIALISTS)}",
            err=True,
        )
        raise typer.Exit(2)
    cfg = _config(db)
    trace = _get_trace_or_exit(trace_id, cfg, project)
    registry = _pricing_registry(cfg)
    try:
        # server-side objects can never arrive over a flag — inject the
        # registry like the API does, never a wire/dead value
        run = run_specialist(
            specialist, trace,
            context={"pricing": registry},
            run_id=f"{specialist}:{trace_id}",
        )
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        typer.echo(f"specialist rejected context: {exc}", err=True)
        raise typer.Exit(2) from None
    out = {
        "run_id": run.run_id,
        "specialist": run.specialist,
        "stop_reason": run.stop_reason,
        "iterations": run.iterations,
        "tokens_observed": run.tokens_observed,
        "cancelled": run.cancelled,
        "findings": run.findings,
    }
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        f"run {out['run_id']}  stop={out['stop_reason']}  "
        f"iterations={out['iterations']}  tokens={out['tokens_observed']}"
    )
    for finding in run.findings:
        typer.echo(json.dumps(finding, indent=2, sort_keys=True, default=str))


@app.command()
def audit(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    action: Annotated[
        str | None, typer.Option("--action", help="exact action filter")
    ] = None,
    actor: Annotated[
        str | None, typer.Option("--actor", help="exact actor filter")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Audit trail rows (``GET /v1/admin/audit``).

    Events only exist when auth was enabled — an empty trail is a
    normal state, not an error. Events carry no project attribution,
    so there is no ``--project`` filter (same as the API)."""
    cfg = _config(db)
    if not 0 <= limit <= 500:
        typer.echo("invalid --limit: 0..500 expected", err=True)
        raise typer.Exit(2)
    _check_i64_opt(offset, "--offset")
    store = _store(cfg)
    try:
        items = store.list_audit(
            action=action, actor=actor, limit=limit, offset=offset
        )
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        typer.echo("no audit events recorded")
        return
    for e in items:
        ts = (e["ts"] or "?")[:19]
        typer.echo(
            f"{ts}  {e['actor']:<12} {e['action']:<24} "
            f"{e['resource']:<24} {e['outcome']}"
        )


# -- experiments (Cycle 13): persisted A/B eval specs ----------------------

_EXPERIMENT_MAX_TRACE_IDS = 500
_EXPERIMENT_MAX_SPECS = 100


def _experiment_eval_specs(raw: Any) -> list[Any]:
    """``--evaluators-json`` → spec list — same contract as the API's
    ``_eval_specs`` plus the per-spec dict/string-name check the runner
    itself relies on."""
    if (
        not isinstance(raw, list)
        or not raw
        or len(raw) > _EXPERIMENT_MAX_SPECS
    ):
        typer.echo(
            f"--evaluators-json: 1..{_EXPERIMENT_MAX_SPECS} specs required",
            err=True,
        )
        raise typer.Exit(2)
    for spec in raw:
        if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
            typer.echo(
                "--evaluators-json: every spec must be an object with "
                "a string 'name'",
                err=True,
            )
            raise typer.Exit(2)
    return raw


def _experiment_cohort(cohorts: Any, key: str) -> dict[str, Any]:
    """One stored cohort — mirrors ``api/app.py::_experiment_cohort``:
    ``{trace_ids: 1..500 strings, label?, config?}``."""
    if not isinstance(cohorts, dict):
        typer.echo(
            "--cohorts-json: object with 'a' and 'b' required", err=True
        )
        raise typer.Exit(2)
    side = cohorts.get(key)
    if not isinstance(side, dict):
        typer.echo(f"cohorts.{key}: object required", err=True)
        raise typer.Exit(2)
    ids = side.get("trace_ids")
    if (
        not isinstance(ids, list)
        or not ids
        or len(ids) > _EXPERIMENT_MAX_TRACE_IDS
        or any(not isinstance(t, str) for t in ids)
    ):
        typer.echo(
            f"cohorts.{key}.trace_ids: 1..{_EXPERIMENT_MAX_TRACE_IDS} "
            "strings required",
            err=True,
        )
        raise typer.Exit(2)
    label = side.get("label")
    if label is not None and not isinstance(label, str):
        typer.echo(f"cohorts.{key}.label: string required", err=True)
        raise typer.Exit(2)
    config = side.get("config")
    if config is not None and not isinstance(config, dict):
        typer.echo(f"cohorts.{key}.config: object required", err=True)
        raise typer.Exit(2)
    return {"trace_ids": list(ids), "label": label, "config": config or {}}


def _reject_non_finite(obj: Any) -> None:
    """NaN/Infinity in a *declared* spec is dishonest data — reject at
    the boundary (mirrors the API's ``_reject_non_finite``)."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, float) and not math.isfinite(cur):
            typer.echo(
                "non-finite number in experiment spec", err=True
            )
            raise typer.Exit(2)
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


@experiments_app.command("create")
def experiments_create(
    experiment_id: Annotated[str, typer.Argument()],
    name: Annotated[str | None, typer.Option("--name")] = None,
    description: Annotated[
        str | None, typer.Option("--description")
    ] = None,
    dataset_id: Annotated[str | None, typer.Option("--dataset-id")] = None,
    dataset_version: Annotated[
        str | None, typer.Option("--dataset-version")
    ] = None,
    evaluators_json: Annotated[
        str,
        typer.Option(
            "--evaluators-json",
            help="JSON array of evaluator specs (required)",
        ),
    ] = "",
    cohorts_json: Annotated[
        str,
        typer.Option(
            "--cohorts-json",
            help="JSON object {a: {trace_ids,label?,config?}, b: {...}} (required)",
        ),
    ] = "",
    metadata_json: Annotated[
        str | None, typer.Option("--metadata-json")
    ] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create/replace an experiment spec (``PUT /v1/experiments/{id}``).

    The spec pins cohorts + evaluator specs so runs are reproducible;
    every ``experiments run`` appends to an append-only result list."""
    specs = _experiment_eval_specs(_json_option(
        evaluators_json, "--evaluators-json"))
    cohorts_raw = _json_option(cohorts_json, "--cohorts-json")
    cohorts = {
        "a": _experiment_cohort(cohorts_raw, "a"),
        "b": _experiment_cohort(cohorts_raw, "b"),
    }
    if name is not None:
        name = name.strip()
        if not name:
            typer.echo("--name: non-empty string required", err=True)
            raise typer.Exit(2)
    if dataset_version is not None and dataset_id is None:
        typer.echo("--dataset-version requires --dataset-id", err=True)
        raise typer.Exit(2)
    metadata = (
        _json_option(metadata_json, "--metadata-json")
        if metadata_json is not None
        else None
    )
    if metadata is not None and not isinstance(metadata, dict):
        typer.echo("--metadata-json: object required", err=True)
        raise typer.Exit(2)
    _reject_non_finite(
        {"evaluators": specs, "cohorts": cohorts, "metadata": metadata}
    )
    cfg = _config(db)
    pid = project or cfg.project_id
    store = _store(cfg)
    try:
        store.upsert_experiment(
            experiment_id,
            project_id=pid,
            name=name,
            description=description,
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            cohorts=cohorts,
            evaluators=list(specs),
            metadata=metadata,
        )
    finally:
        store.close()
    out = {"experiment_id": experiment_id, "project_id": pid, "stored": True}
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(f"experiment {experiment_id} stored in project {pid}")


@experiments_app.command("list")
def experiments_list(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 100,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List experiment specs (``GET /v1/experiments``)."""
    cfg = _config(db)
    _check_i64_opt(offset, "--offset")
    store = _store(cfg)
    try:
        items = store.list_experiments(
            project or cfg.project_id,
            limit=max(1, min(500, limit)),
            offset=offset,
        )
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(items, indent=2))
        return
    if not items:
        typer.echo("no experiments recorded")
        return
    for e in items:
        created = (e["created_at"] or "?")[:19]
        name = e["name"] or "-"
        typer.echo(
            f"{e['experiment_id']:<24} {name:<24} "
            f"status={e['status']:<10} created={created}"
        )


@experiments_app.command("show")
def experiments_show(
    experiment_id: Annotated[str, typer.Argument()],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 500,
    offset: Annotated[int, typer.Option("--offset")] = 0,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Experiment spec + append-only run history
    (``GET /v1/experiments/{id}``)."""
    cfg = _config(db)
    _check_i64_opt(offset, "--offset")
    store = _store(cfg)
    try:
        pid = project or cfg.project_id
        exp = store.get_experiment(experiment_id, pid)
        if exp is None:
            typer.echo(f"experiment {experiment_id!r} not found", err=True)
            raise typer.Exit(2)
        runs = store.list_experiment_results(
            experiment_id, pid,
            limit=max(1, min(500, limit)),
            offset=offset,
        )
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(
            {**exp, "runs": runs, "run_limit": limit, "run_offset": offset},
            indent=2,
        ))
        return
    typer.echo(
        f"experiment {exp['experiment_id']}  project={exp['project_id']}  "
        f"status={exp['status']}"
    )
    if exp["name"]:
        typer.echo(f"  name:        {exp['name']}")
    if exp["description"]:
        typer.echo(f"  description: {exp['description']}")
    if exp["dataset_id"]:
        ds = exp["dataset_id"]
        if exp["dataset_version"]:
            ds += f"@{exp['dataset_version']}"
        typer.echo(f"  dataset:     {ds}")
    typer.echo(
        f"  cohorts:     a={len((exp['cohorts'].get('a') or {}).get('trace_ids') or [])} "
        f"b={len((exp['cohorts'].get('b') or {}).get('trace_ids') or [])} trace_ids"
    )
    typer.echo(f"  evaluators:  {len(exp['evaluators'])} spec(s)")
    if not runs:
        typer.echo("  no runs recorded")
    for r in runs:
        at = (r["run_at"] or "?")[:19]
        deltas = len(r["report"].get("deltas") or [])
        typer.echo(f"  run {r['run_id']:<6} {at}  deltas={deltas}")


@experiments_app.command("run")
def experiments_run(
    experiment_id: Annotated[str, typer.Argument()],
    gate_json: Annotated[
        str | None,
        typer.Option("--gate-json", help="JSON gate spec (eval.compare)"),
    ] = None,
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Execute the stored spec through the same compare path as
    ``POST /v1/experiments/{id}/run`` (resolve cohorts →
    evaluate_traces → compare_runs → optional gate), then append the
    report to ``experiment_results``."""
    from forge_lens.api.clock import wall_clock_ns
    from forge_lens.core.time import from_unix_nano
    from forge_lens.eval import compare_runs, evaluate_traces

    gate = _json_option(gate_json, "--gate-json") if gate_json is not None else None
    if gate is not None and not isinstance(gate, dict):
        typer.echo("--gate-json: object required", err=True)
        raise typer.Exit(2)
    cfg = _config(db)
    pid = project or cfg.project_id
    store = _store(cfg)
    try:
        exp = store.get_experiment(experiment_id, pid)
        if exp is None:
            typer.echo(f"experiment {experiment_id!r} not found", err=True)
            raise typer.Exit(2)
        specs = exp["evaluators"]
        if not specs:
            # unreachable via create (1..100 enforced) but a spec
            # written outside it must fail loudly, not run a null eval
            typer.echo(
                f"experiment {experiment_id!r} has no evaluators spec",
                err=True,
            )
            raise typer.Exit(2)
        cohorts = exp["cohorts"]
        label = (
            f"{exp['dataset_id']}@{exp['dataset_version']}"
            if exp.get("dataset_id") and exp.get("dataset_version")
            else exp.get("dataset_id")
        )

        def _cohort(key: str) -> list[Trace]:
            out: list[Trace] = []
            for tid in (cohorts.get(key) or {}).get("trace_ids") or []:
                t = store.get_trace(tid, project_id=pid)
                if t is None:
                    typer.echo(f"trace {tid} not found", err=True)
                    raise typer.Exit(2)
                out.append(t)
            return out

        result = compare_runs(
            evaluate_traces(_cohort("a"), specs, dataset=label),
            evaluate_traces(_cohort("b"), specs, dataset=label),
        )
        if gate is not None:
            from forge_lens.eval.compare import apply_gate

            try:
                result["gate"] = apply_gate(result, gate)
            except (ValueError, TypeError) as exc:
                typer.echo(f"gate: {exc}", err=True)
                raise typer.Exit(2) from None
        run_ns = wall_clock_ns()
        run_id = store.record_experiment_result(
            experiment_id,
            project_id=pid,
            report=result,
            run_ns=run_ns,
        )
    finally:
        store.close()
    out = {
        "experiment_id": experiment_id,
        "project_id": pid,
        "run_id": run_id,
        "run_at": from_unix_nano(run_ns).isoformat(),
        "report": result,
    }
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        f"run {run_id} recorded for experiment {experiment_id} "
        f"at {out['run_at']}"
    )
    for d in result.get("deltas") or []:
        delta = "n/a" if d.get("delta") is None else f"{d['delta']:g}"
        typer.echo(
            f"  {d['evaluator']}/{d['metric']}: "
            f"a={d.get('mean_a')} b={d.get('mean_b')} delta={delta} "
            f"({d.get('significance')})"
        )
    if result.get("gate") is not None:
        gate_out = result["gate"]
        typer.echo(f"gate: passed={gate_out['gate_passed']}")
        for f in gate_out.get("failures") or []:
            typer.echo(f"  ~ {f.get('metric') or 'gate'}: {f.get('reason')}")


# -- alerts (Cycle 07 §07.5): threshold rules over store metrics -----------


def _alert_rules_path(cfg: LensConfig) -> Path:
    return cfg.data_dir / "alert-rules.json"


@alerts_app.command("list")
def alerts_list(
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List rules persisted in <data-dir>/alert-rules.json."""
    from forge_lens.ops.alerts import load_rules

    rules = load_rules(_alert_rules_path(_config()))
    if json_out:
        typer.echo(json.dumps(rules, indent=2))
        return
    if not rules:
        typer.echo("no alert rules saved")
        return
    for r in rules:
        typer.echo(
            f"{r['rule_id']:<24} {r.get('metric', '-'):<24} "
            f"{r.get('op', '?')} {r.get('threshold', '?')}  "
            f"severity={r.get('severity', 'warning')}"
        )


@alerts_app.command("save")
def alerts_save(
    rule_json: Annotated[
        str,
        typer.Option("--rule-json", help="JSON rule object (required)"),
    ] = "",
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Insert/replace a rule in <data-dir>/alert-rules.json
    (``POST /v1/alerts/rules`` — durable, local, atomic write)."""
    from forge_lens.ops.alerts import load_rules, save_rule

    rule = _json_option(rule_json, "--rule-json")
    if not isinstance(rule, dict) or not isinstance(rule.get("rule_id"), str):
        typer.echo(
            "--rule-json: object with a string 'rule_id' required",
            err=True,
        )
        raise typer.Exit(2)
    path = _alert_rules_path(_config())
    # a saved file >100 rules would 422 every use_saved evaluation
    # forever — enforce the eval cap on writes too (replace ≠ grow)
    existing = load_rules(path)
    if (
        not any(r["rule_id"] == rule["rule_id"] for r in existing)
        and len(existing) >= 100
    ):
        typer.echo("saved rules cap is 100 — delete one first", err=True)
        raise typer.Exit(2)
    rules = save_rule(path, rule)
    out = {"rules": rules, "path": str(path)}
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(f"saved {rule['rule_id']} → {path} ({len(rules)} rule(s))")


@alerts_app.command("delete")
def alerts_delete(
    rule_id: Annotated[str, typer.Argument()],
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Delete a persisted rule (``DELETE /v1/alerts/rules/{id}``)."""
    from forge_lens.ops.alerts import delete_rule, load_rules

    path = _alert_rules_path(_config())
    if not any(r["rule_id"] == rule_id for r in load_rules(path)):
        typer.echo(f"rule {rule_id!r} not found", err=True)
        raise typer.Exit(2)
    rules = delete_rule(path, rule_id)
    if json_out:
        typer.echo(json.dumps({"rules": rules}, indent=2))
        return
    typer.echo(f"deleted {rule_id} ({len(rules)} rule(s) left)")


@alerts_app.command("evaluate")
def alerts_evaluate(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    limit: Annotated[
        int,
        typer.Option(
            "--limit", "-n",
            help="recent-trace window for derived metrics (≤500)",
        ),
    ] = 200,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Evaluate the saved rules against live store metrics
    (``POST /v1/alerts/evaluate`` with ``use_saved``).

    Metrics are computed from the store at call time — stats counters
    for the whole project plus a bounded recent-trace window for
    duration/cost/token/loop metrics."""
    from forge_lens.ops.alerts import (
        evaluate_rules,
        load_rules,
        resolve_metrics,
    )

    cfg = _config(db)
    pid = project or cfg.project_id
    rules = load_rules(_alert_rules_path(cfg))
    store = _store(cfg)
    try:
        stats = store.stats(project_id=pid)
        spans_total = int(stats.get("spans") or 0)
        errors = int(stats.get("error_spans") or 0)
        metrics = {
            "span_count": float(spans_total),
            "trace_count": float(stats.get("traces") or 0),
            "error_count": float(errors),
            "error_rate": errors / spans_total if spans_total else 0.0,
        }
        # derived metrics (durations/cost/tokens/loops) resolve over a
        # bounded recent-trace window — ops/alerts.py documents each
        metrics.update(resolve_metrics(
            store, pid, _pricing_registry(cfg),
            window=max(0, min(500, limit)),
            only={str(r.get("metric")) for r in rules if isinstance(r, dict)},
        ))
    finally:
        store.close()
    out = evaluate_rules([r for r in rules if isinstance(r, dict)], metrics)
    out["project_id"] = pid
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    fired = out["fired"]
    typer.echo(
        f"evaluated {out['evaluated']} rule(s) — {len(fired)} fired"
    )
    for f in fired:
        typer.echo(
            f"  [{f.get('severity', 'warning')}] {f['rule_id']}: {f['message']}"
        )


# -- eval-suggest (Cycle 12 §12.7): suggestions from failure evidence ------


@app.command(name="eval-suggest")
def eval_suggest(
    trace_ids: Annotated[
        list[str],
        typer.Argument(help="one or more trace ids", metavar="TRACE_ID..."),
    ],
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Candidate evaluator/dataset suggestions from failure evidence
    across ≥1 stored traces (Cycle 12 §12.7).

    With ≥2 traces sharing a failure category the ``evidence.recurring``
    flag marks the pattern — suggestions are ``candidate``-classified:
    review and apply by hand; datasets are never modified."""
    from forge_lens.investigate import classify_failure, suggest_evaluators

    if not trace_ids:
        typer.echo(
            "usage: forge-lens eval-suggest <trace_id> [trace_id...]",
            err=True,
        )
        raise typer.Exit(2)
    cfg = _config(db)
    pid = project or cfg.project_id
    store = _store(cfg)
    try:
        reports = []
        for tid in trace_ids:
            trace = store.get_trace(tid, project_id=pid)
            if trace is None:
                typer.echo(f"trace {tid} not found", err=True)
                raise typer.Exit(2)
            reports.append(classify_failure(trace))
    finally:
        store.close()
    out = suggest_evaluators(reports)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    suggestions = out["suggestions"]
    if not suggestions:
        typer.echo(
            "no eval suggestions — no classifiable failure evidence "
            "in the given trace(s)"
        )
        return
    typer.echo(
        f"{len(suggestions)} candidate suggestion(s) "
        f"across {len(trace_ids)} trace(s) — {out['note']}"
    )
    # grouped by evaluator/dataset-case name (sorted, like the payload)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for s in suggestions:
        by_name.setdefault(str(s["name"]), []).append(s)
    for name in sorted(by_name):
        typer.echo(f"  {name}:")
        for s in by_name[name]:
            ev = s["evidence"]
            recurring = " recurring" if ev.get("recurring") else ""
            typer.echo(
                f"    [{s['kind']}] ({ev.get('count', 0)} trace(s){recurring}) "
                f"{s['rationale']}"
            )
            if s.get("params"):
                typer.echo(f"      params: {json.dumps(s['params'], sort_keys=True)}")
            if s.get("params_required"):
                typer.echo(
                    f"      params_required: {', '.join(s['params_required'])}"
                )


# -- intel (Cycles 12/13): fleet-level intelligence -------------------------


@intel_app.command("efficiency")
def intel_efficiency(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    session: Annotated[
        str | None, typer.Option("--session", help="exact session_id")
    ] = None,
    tool: Annotated[
        str | None,
        typer.Option("--tool", help="substring match on span tool.name"),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 200,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Fleet efficiency roll-up over a bounded, optionally
    session/tool-filtered window (``POST /v1/intel/efficiency``)."""
    from forge_lens.intel.efficiency import fleet_efficiency

    cfg = _config(db)
    pid = project or cfg.project_id
    store = _store(cfg)
    try:
        rows = store.list_traces(
            project_id=pid,
            session_id=session,
            tool=tool,
            limit=max(0, min(500, limit)),
        )
        traces = [
            t
            for r in rows
            if (t := store.get_trace(r["trace_id"], project_id=pid))
            is not None
        ]
    finally:
        store.close()
    out = fleet_efficiency(traces)
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        f"fleet: {out['traces']} trace(s), {out['succeeded']} succeeded"
    )
    for key in (
        "success_rate", "tokens_per_trace", "tokens_per_success",
        "tool_calls_per_trace", "tool_calls_per_success",
        "retry_overhead", "error_rate",
    ):
        v = out[key]
        typer.echo(f"  {key}: {'n/a' if v is None else f'{v:g}'}")
    typer.echo(f"  note: {out['note']}")


@intel_app.command("correlate")
def intel_correlate(
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 200,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Fleet co-occurrence tables over a bounded window
    (``POST /v1/intel/correlate``) — descriptive co-occurrence, never
    causation; small cohorts are labelled ``insufficient_data``."""
    from forge_lens.intel.correlation import correlate_fleet

    cfg = _config(db)
    pid = project or cfg.project_id
    store = _store(cfg)
    try:
        out = correlate_fleet(store, pid, limit=max(0, min(500, limit)))
    finally:
        store.close()
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    w = out["window"]
    oc = out["outcomes"]
    typer.echo(
        f"window: {w['traces']} trace(s) (limit {w['limit_applied']}) — "
        f"failed={oc['failed']} ok={oc['ok']} other={oc['other']}"
    )
    for name, table in sorted(out["tables"].items()):
        typer.echo(f"  {name}: {table.get('classification', '-')}")
        for row in table.get("rows", [])[:10]:
            key = row.get("tool") or row.get("model") or row.get("bucket") or "?"
            extra = ""
            if row.get("rate_difference_pp") is not None:
                extra = f"  Δ={row['rate_difference_pp']}pp"
            elif row.get("score_delta_vs_window") is not None:
                extra = f"  Δ={row['score_delta_vs_window']}"
            typer.echo(f"    {key}{extra}  ({row.get('classification', '-')})")
    typer.echo(f"  note: {out['note']}")


@intel_app.command("regress")
def intel_regress(
    a_json: Annotated[
        str,
        typer.Option("--a-json", help="JSON array of baseline trace_ids"),
    ] = "",
    b_json: Annotated[
        str,
        typer.Option("--b-json", help="JSON array of candidate trace_ids"),
    ] = "",
    db: Annotated[Path | None, typer.Option("--db")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    json_out: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Cohort regression between two trace-id lists
    (``POST /v1/intel/regress``) — observed deltas, not
    significance-tested (``eval compare`` owns the statistics)."""
    from forge_lens.intel.regressions import compare_cohorts

    def _side(raw: str, opt: str) -> list[str]:
        parsed = _json_option(raw, opt)
        if (
            not isinstance(parsed, list)
            or not parsed
            or len(parsed) > _EXPERIMENT_MAX_TRACE_IDS
            or any(not isinstance(t, str) for t in parsed)
        ):
            typer.echo(
                f"{opt}: 1..{_EXPERIMENT_MAX_TRACE_IDS} trace-id strings "
                "required",
                err=True,
            )
            raise typer.Exit(2)
        return list(parsed)

    a_ids = _side(a_json, "--a-json")
    b_ids = _side(b_json, "--b-json")
    cfg = _config(db)
    pid = project or cfg.project_id
    store = _store(cfg)
    try:
        a_traces: list[Trace] = []
        b_traces: list[Trace] = []
        missing: dict[str, list[str]] = {"a": [], "b": []}
        for key, ids, acc in (("a", a_ids, a_traces), ("b", b_ids, b_traces)):
            for tid in ids:
                t = store.get_trace(tid, project_id=pid)
                if t is None:
                    missing[key].append(tid)
                else:
                    acc.append(t)
    finally:
        store.close()
    out = compare_cohorts(a_traces, b_traces, label="cohort")
    if missing["a"] or missing["b"]:
        out["missing_trace_ids"] = missing
    if json_out:
        typer.echo(json.dumps(out, indent=2))
        return
    typer.echo(
        f"cohort: base={out['base_traces']} candidate={out['candidate_traces']} "
        f"trace(s) — {out['classification']}"
    )
    for m, d in out["deltas"].items():
        if d.get("delta") is None:
            typer.echo(f"  {m}: {d.get('reason', 'n/a')}")
        else:
            pct = "n/a" if d.get("pct") is None else f"{d['pct']:+.2f}%"
            typer.echo(
                f"  {m}: {d['base']:g} → {d['candidate']:g}  "
                f"delta={d['delta']:g} ({pct})"
            )
    if missing["a"] or missing["b"]:
        typer.echo(f"  ~ missing_trace_ids: {json.dumps(missing, sort_keys=True)}")
    typer.echo(f"  note: {out['note']}")


if __name__ == "__main__":
    app()
