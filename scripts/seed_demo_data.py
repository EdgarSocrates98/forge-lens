#!/usr/bin/env python3
"""Seed a deterministic demo dataset for the Studio E2E suite.

Writes ~10 traces (two sessions, one failure run, tool calls, token
usage, span events), one eval dataset with a versioned case set, and one
experiment spec into a Forge Lens SQLite store — through the same
``IngestPipeline`` the API uses, so seeded rows look exactly like real
ingest.

Determinism: every id derives from sha256 of a logical name and every
timestamp is a fixed offset from a frozen base instant — reseeding an
empty db reproduces byte-identical rows, and the emitted manifest lets
the Playwright specs deep-link without guessing ids.

Run from the repo root:

    .venv/bin/python scripts/seed_demo_data.py \
        --db apps/studio/e2e/.runtime/lens.db \
        --manifest apps/studio/e2e/.runtime/seed-manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from forge_lens.contracts.v1.trace import (
    Span,
    SpanEvent,
    SpanKind,
    Status,
    StatusCode,
    Trace,
    TraceSource,
)
from forge_lens.core.config import LensConfig
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.storage.sqlite import SQLiteTraceStore

# Frozen clock — every timestamp derives from this instant so the seed
# is reproducible and the traces view's newest-first ordering is stable.
BASE = datetime(2025, 6, 15, 12, 0, 0, tzinfo=UTC)

SOURCE = TraceSource(
    system="studio-e2e-seed",
    sdk_name="seed_demo_data.py",
    runtime="python",
    language="python",
)

SESSION_ALPHA = "session-demo-alpha"
SESSION_BETA = "session-demo-beta"
DATASET_ID = "ds-demo-qa"
DATASET_VERSION = "v1"
EXPERIMENT_ID = "exp-demo-ab"


def tid(name: str) -> str:
    """Logical name → deterministic 32-hex trace id."""
    return hashlib.sha256(f"forge-lens-e2e/trace/{name}".encode()).hexdigest()[:32]


def sid(name: str) -> str:
    """Logical name → deterministic 16-hex span id."""
    return hashlib.sha256(f"forge-lens-e2e/span/{name}".encode()).hexdigest()[:16]


def _at(offset_ms: int) -> datetime:
    return BASE + timedelta(milliseconds=offset_ms)


def _usage(model: str, inp: int, out: int) -> dict[str, Any]:
    """Same attribute shape LensTracer.record_usage writes."""
    return {
        "provider": "anthropic",
        "model": model,
        "input_tokens": inp,
        "output_tokens": out,
        "total_tokens": inp + out,
        "measurement_source": "provider",
    }


def _span(
    name: str,
    trace_id: str,
    *,
    kind: SpanKind,
    parent: str | None,
    start_ms: int,
    end_ms: int,
    status: StatusCode = StatusCode.OK,
    message: str | None = None,
    attributes: dict[str, Any] | None = None,
    events: list[SpanEvent] | None = None,
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=sid(name),
        parent_span_id=parent,
        name=name,
        kind=kind,
        start_time=_at(start_ms),
        end_time=_at(end_ms),
        status=Status(code=status, message=message),
        attributes=attributes or {},
        events=events or [],
    )


def _trace(
    name: str,
    spans: list[Span],
    *,
    session_id: str | None,
    status: StatusCode,
) -> Trace:
    starts = [s.start_time for s in spans]
    ends = [s.end_time for s in spans if s.end_time is not None]
    root = next(s for s in spans if s.parent_span_id is None)
    return Trace(
        trace_id=tid(name),
        project_id="default",
        session_id=session_id,
        root_span_id=root.span_id,
        started_at=min(starts),
        ended_at=max(ends) if ends else None,
        status=Status(code=status),
        source=SOURCE,
        resource_attributes={"service.name": "demo-agent"},
        spans=spans,
    )


def build_traces() -> list[Trace]:
    """The seeded fleet — every trace the specs assert against."""
    traces: list[Trace] = []

    # -- session alpha: three research-agent runs ----------------------
    t = tid("alpha-research-1")
    root = _span("research-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=0, end_ms=1200,
                 attributes={"gen_ai.agent.name": "research-agent"})
    traces.append(_trace("alpha-research-1", [
        root,
        _span("llm claude-demo-3", t, kind=SpanKind.LLM,
              parent=root.span_id, start_ms=50, end_ms=450,
              attributes={
                  "llm.model_name": "claude-demo-3",
                  "llm.provider": "anthropic",
                  "forge_lens.token_usage": _usage("claude-sonnet-4", 120, 40),
              }),
        _span("tool web_search", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=500, end_ms=650,
              attributes={"tool.name": "web_search"}),
        _span("tool read_file", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=700, end_ms=780,
              attributes={"tool.name": "read_file"}),
    ], session_id=SESSION_ALPHA, status=StatusCode.OK))

    t = tid("alpha-research-2")
    root = _span("research-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=60_000, end_ms=60_900,
                 attributes={"gen_ai.agent.name": "research-agent"})
    traces.append(_trace("alpha-research-2", [
        root,
        _span("llm claude-demo-3", t, kind=SpanKind.LLM,
              parent=root.span_id, start_ms=60_050, end_ms=60_400,
              attributes={
                  "llm.model_name": "claude-demo-3",
                  "llm.provider": "anthropic",
                  "forge_lens.token_usage": _usage("claude-sonnet-4", 300, 90),
              }),
        _span("tool web_search", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=60_450, end_ms=60_800,
              attributes={"tool.name": "web_search"}),
    ], session_id=SESSION_ALPHA, status=StatusCode.OK))

    # richer tree — exercises the waterfall depth + the `q` span-name
    # filter ("rag-pipeline")
    t = tid("alpha-research-3")
    root = _span("research-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=120_000, end_ms=121_800,
                 attributes={"gen_ai.agent.name": "research-agent"})
    chain = _span("rag-pipeline", t, kind=SpanKind.CHAIN,
                  parent=root.span_id, start_ms=120_100, end_ms=121_600)
    traces.append(_trace("alpha-research-3", [
        root,
        chain,
        _span("retriever docs", t, kind=SpanKind.RETRIEVER,
              parent=chain.span_id, start_ms=120_200, end_ms=120_700,
              attributes={"retriever.top_k": 5}),
        _span("llm claude-demo-3", t, kind=SpanKind.LLM,
              parent=chain.span_id, start_ms=120_800, end_ms=121_500,
              attributes={
                  "llm.model_name": "claude-demo-3",
                  "llm.provider": "anthropic",
                  "forge_lens.token_usage": _usage("claude-sonnet-4", 850, 210),
              }),
    ], session_id=SESSION_ALPHA, status=StatusCode.OK))

    # -- session beta: one ok run + the seeded failure ------------------
    t = tid("beta-task-1")
    root = _span("task-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=180_000, end_ms=181_000,
                 attributes={"gen_ai.agent.name": "task-agent"})
    traces.append(_trace("beta-task-1", [
        root,
        _span("tool http_fetch", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=180_100, end_ms=180_500,
              attributes={"tool.name": "http_fetch"}),
        _span("llm claude-demo-3", t, kind=SpanKind.LLM,
              parent=root.span_id, start_ms=180_600, end_ms=180_950,
              attributes={
                  "llm.model_name": "claude-demo-3",
                  "llm.provider": "anthropic",
                  "forge_lens.token_usage": _usage("claude-sonnet-4", 200, 60),
              }),
    ], session_id=SESSION_BETA, status=StatusCode.OK))

    # THE failure trace — ops "recent failures" + error-filter target.
    # The error span carries an exception event, like the SDK records.
    t = tid("beta-fail")
    root = _span("task-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=240_000, end_ms=241_200,
                 status=StatusCode.ERROR,
                 attributes={"gen_ai.agent.name": "task-agent"})
    bad_tool = _span(
        "tool http_fetch", t, kind=SpanKind.TOOL,
        parent=root.span_id, start_ms=240_100, end_ms=240_900,
        status=StatusCode.ERROR, message="TimeoutError: connect timed out",
        attributes={"tool.name": "http_fetch"},
        events=[SpanEvent(
            name="exception",
            time=_at(240_900),
            attributes={
                "exception.type": "TimeoutError",
                "exception.message": "connect timed out",
            },
        )],
    )
    traces.append(_trace("beta-fail", [
        root,
        bad_tool,
        _span("llm claude-demo-3", t, kind=SpanKind.LLM,
              parent=root.span_id, start_ms=241_000, end_ms=241_150,
              attributes={
                  "llm.model_name": "claude-demo-3",
                  "llm.provider": "anthropic",
                  "forge_lens.token_usage": _usage("claude-sonnet-4", 60, 20),
              }),
    ], session_id=SESSION_BETA, status=StatusCode.ERROR))

    # -- standalone traces (no session) --------------------------------
    # slow trace — only one ≥3000ms, for the min_duration filter
    t = tid("standalone-slow")
    root = _span("batch-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=300_000, end_ms=305_000,
                 attributes={"gen_ai.agent.name": "batch-agent"})
    traces.append(_trace("standalone-slow", [
        root,
        _span("llm claude-demo-3", t, kind=SpanKind.LLM,
              parent=root.span_id, start_ms=300_200, end_ms=304_800,
              attributes={
                  "llm.model_name": "claude-demo-3",
                  "llm.provider": "anthropic",
                  "forge_lens.token_usage": _usage("claude-sonnet-4", 2400, 800),
              }),
        _span("tool vector_db_query", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=301_000, end_ms=302_500,
              attributes={"tool.name": "vector_db_query"}),
    ], session_id=None, status=StatusCode.OK))

    # unset-status trace — ended but never marked ok/error
    t = tid("standalone-unset")
    root = _span("observer", t, kind=SpanKind.INTERNAL, parent=None,
                 start_ms=360_000, end_ms=360_400,
                 status=StatusCode.UNSET)
    traces.append(_trace("standalone-unset", [root],
                         session_id=None, status=StatusCode.UNSET))

    # span events → replay 'event' entries (enables the "next event" jump)
    t = tid("standalone-events")
    root = _span("chatty-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=420_000, end_ms=421_000,
                 attributes={"gen_ai.agent.name": "chatty-agent"})
    traces.append(_trace("standalone-events", [
        root,
        _span("llm claude-demo-3", t, kind=SpanKind.LLM,
              parent=root.span_id, start_ms=420_100, end_ms=420_900,
              attributes={
                  "llm.model_name": "claude-demo-3",
                  "llm.provider": "anthropic",
                  "forge_lens.token_usage": _usage("claude-sonnet-4", 150, 55),
              },
              events=[
                  SpanEvent(name="retry", time=_at(420_300),
                            attributes={"attempt": 1}),
                  SpanEvent(name="cache_hit", time=_at(420_500)),
              ]),
    ], session_id=None, status=StatusCode.OK))

    # multi-tool trace — `tool=calculator` filters down to this one only
    t = tid("standalone-tools-multi")
    root = _span("toolbox-agent", t, kind=SpanKind.AGENT, parent=None,
                 start_ms=480_000, end_ms=481_500,
                 attributes={"gen_ai.agent.name": "toolbox-agent"})
    traces.append(_trace("standalone-tools-multi", [
        root,
        _span("tool web_search", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=480_100, end_ms=480_400,
              attributes={"tool.name": "web_search"}),
        _span("tool calculator", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=480_500, end_ms=480_700,
              attributes={"tool.name": "calculator"}),
        _span("tool code_exec", t, kind=SpanKind.TOOL,
              parent=root.span_id, start_ms=480_800, end_ms=481_400,
              attributes={"tool.name": "code_exec"}),
    ], session_id=None, status=StatusCode.OK))

    # minimal single-span trace — small waterfall contrast
    t = tid("standalone-mini")
    root = _span("ping", t, kind=SpanKind.INTERNAL, parent=None,
                 start_ms=540_000, end_ms=540_050)
    traces.append(_trace("standalone-mini", [root],
                         session_id=None, status=StatusCode.OK))

    return traces


def seed(db_path: Path, manifest_path: Path | None) -> dict[str, Any]:
    if db_path.exists():
        db_path.unlink()
    for wal in (db_path.with_suffix(db_path.suffix + "-wal"),
                db_path.with_suffix(db_path.suffix + "-shm")):
        if wal.exists():
            wal.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    cfg = LensConfig(db_path=db_path)
    store = SQLiteTraceStore(db_path)
    pipeline = IngestPipeline(store, cfg)

    traces = build_traces()
    result = pipeline.ingest(traces)

    # eval dataset + one append-only version with cases
    now_ns = int(BASE.timestamp() * 1_000_000_000)
    store.upsert_dataset(
        DATASET_ID,
        project_id="default",
        name="Demo QA set",
        task="question-answering",
        metadata={"seeded_by": "seed_demo_data.py"},
        now_ns=now_ns,
    )
    store.upsert_dataset_version(
        DATASET_ID,
        DATASET_VERSION,
        project_id="default",
        description="seeded demo cases",
        cases=[
            {
                "case_id": "c-1",
                "input": {"q": "what is otel?"},
                "expected": {"output.value": "observability telemetry"},
            },
            {
                "case_id": "c-2",
                "input": {"q": "name a span kind"},
                "expected": {"output.value": "llm"},
            },
        ],
        now_ns=now_ns,
    )

    # experiment spec — cohorts pin seeded trace ids; the E2E suite
    # runs it via "run now" (append-only history starts empty)
    store.upsert_experiment(
        EXPERIMENT_ID,
        project_id="default",
        name="Demo A/B",
        description="seeded experiment — baseline vs candidate cohorts",
        dataset_id=DATASET_ID,
        dataset_version=DATASET_VERSION,
        cohorts={
            "a": {
                "trace_ids": [tid("alpha-research-1"), tid("alpha-research-2")],
                "label": "baseline",
            },
            "b": {
                "trace_ids": [tid("beta-task-1"), tid("beta-fail")],
                "label": "candidate",
            },
        },
        evaluators=[{"name": "error_free"}, {"name": "tool_success"}],
        now_ns=now_ns,
    )

    manifest: dict[str, Any] = {
        "project_id": "default",
        "db": str(db_path.resolve()),
        "trace_count": len(traces),
        "span_count": sum(len(t.spans) for t in traces),
        "sessions": {
            "alpha": {"id": SESSION_ALPHA, "traces": 3},
            "beta": {"id": SESSION_BETA, "traces": 2},
        },
        "traces": {t.trace_id: {
            "session_id": t.session_id,
            "status": t.status.code.value,
            "spans": len(t.spans),
        } for t in traces},
        "named": {
            "ok_trace": tid("alpha-research-1"),
            "rich_trace": tid("alpha-research-3"),
            "failing_trace": tid("beta-fail"),
            "slow_trace": tid("standalone-slow"),
            "unset_trace": tid("standalone-unset"),
            "events_trace": tid("standalone-events"),
            "multi_tool_trace": tid("standalone-tools-multi"),
        },
        "tools": sorted({
            str(s.attributes["tool.name"])
            for t in traces for s in t.spans
            if "tool.name" in s.attributes
        }),
        "dataset": {"id": DATASET_ID, "version": DATASET_VERSION, "cases": 2},
        "experiment": {"id": EXPERIMENT_ID},
        "accepted_spans": result.accepted_spans,
        "warnings": result.warnings,
    }
    store.close()

    if manifest_path is not None:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, required=True,
                    help="SQLite db path to (re)create")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="write the seed manifest JSON here")
    args = ap.parse_args()

    manifest = seed(args.db, args.manifest)
    print(
        f"seeded {manifest['trace_count']} traces / "
        f"{manifest['span_count']} spans into {args.db}\n"
        f"  sessions: {SESSION_ALPHA} (3), {SESSION_BETA} (2)\n"
        f"  failing trace: {manifest['named']['failing_trace']}\n"
        f"  dataset: {DATASET_ID}@{DATASET_VERSION} "
        f"({manifest['dataset']['cases']} cases)\n"
        f"  experiment: {EXPERIMENT_ID}"
    )
    if manifest["warnings"]:
        print("  warnings:", *manifest["warnings"], sep="\n    ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
