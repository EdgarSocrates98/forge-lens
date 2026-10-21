"""Benchmark smoke tests — real surfaces, honest wall-times.

Each test asserts *correctness* of the surface's output (exact,
deterministic selectivities from the synthetic corpus) and records the
wall time into the session ``bench`` report. The only timing assertion
is a deliberately generous ceiling on ingest — a tripwire for gross
regressions on loaded CI, never an SLA.

Measured surfaces:

- ``IngestPipeline.ingest`` — validate → sanitize → flag → dedupe →
  persist, on a fresh store (production default ``capture=metadata``).
- ``SQLiteTraceStore.list_traces`` — every server-side filter over a
  seeded 1 000-trace store; ``list_sessions``/``list_tools`` roll-ups;
  ``get_trace`` point reads.
- ``graph.build_trace_graph`` (2 000-span tree) and
  ``graph.build_session_graph`` (one 40-trace session).
- ``eval.evaluate_traces`` — the full 12-evaluator registry over 200
  stored traces, fetch fan-out included (mirrors ``POST /v1/eval/run``).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from forge_lens.core.config import LensConfig
from forge_lens.core.time import to_unix_nano
from forge_lens.eval import EVALUATORS, evaluate_traces
from forge_lens.graph import build_session_graph, build_trace_graph
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.storage.sqlite import SQLiteTraceStore

from .gen import (
    EVAL_SPECS,
    SESSION_COUNT,
    SPANS_PER_TRACE,
    T0,
    TOOL_NAMES,
    make_bench_trace,
    make_tree_trace,
    trace_id_of,
)
from .report import BenchReport, SeededCorpus

pytestmark = pytest.mark.benchmark

INGEST_TRACES = 500
# Smoke ceiling: ~9x headroom over the measured dev-machine time. A
# tripwire for catastrophic regressions, tuned to survive loaded CI —
# this is NOT a performance contract.
INGEST_CEILING_S = 30.0


def test_ingest_pipeline_500_traces(tmp_path: Path, bench: BenchReport) -> None:
    """Ingest 500 traces (2 000 spans) via the real pipeline, fresh DB."""
    store = SQLiteTraceStore(tmp_path / "ingest-bench.db")
    pipeline = IngestPipeline(store, LensConfig())
    traces = [
        make_bench_trace(i, project_id="ingest-bench") for i in range(INGEST_TRACES)
    ]

    t0 = time.perf_counter()
    result = pipeline.ingest(traces)
    elapsed = bench.record(
        "ingest.pipeline",
        time.perf_counter() - t0,
        traces=INGEST_TRACES,
        spans=INGEST_TRACES * SPANS_PER_TRACE,
        capture="metadata",
    )

    assert result.accepted_spans == INGEST_TRACES * SPANS_PER_TRACE
    assert len(result.traces) == INGEST_TRACES
    assert not result.warnings
    # metadata capture drops `output.value` once per trace — proof the
    # sanitize stage actually ran inside the timed window
    assert result.sanitize.get("dropped_content") == INGEST_TRACES
    stats = store.stats()
    assert stats["traces"] == INGEST_TRACES
    assert stats["spans"] == INGEST_TRACES * SPANS_PER_TRACE
    store.close()

    assert elapsed < INGEST_CEILING_S, (
        f"ingest smoke ceiling blown: {elapsed:.1f}s > {INGEST_CEILING_S}s "
        f"for {INGEST_TRACES} traces"
    )


def test_list_traces_page_and_filters(bench_store: SeededCorpus, bench: BenchReport) -> None:
    store, pid = bench_store.store, bench_store.project_id

    with bench.time("store.list_traces[page]", limit=500, seeded=bench_store.traces):
        page = store.list_traces(project_id=pid, limit=500)
    assert len(page) == 500
    # deterministic order: started_ns DESC → newest trace first
    assert page[0]["trace_id"] == trace_id_of(bench_store.traces - 1)

    with bench.time("store.list_traces[status=error]", limit=500):
        errors = store.list_traces(project_id=pid, status="error", limit=500)
    assert len(errors) == bench_store.traces // 10  # i % 10 == 0 → 100
    assert all(r["status"] == "error" for r in errors)

    with bench.time("store.list_traces[session_id]", limit=100):
        sess = store.list_traces(project_id=pid, session_id="session-07", limit=100)
    assert len(sess) == bench_store.traces // SESSION_COUNT  # 40
    assert all(r["session_id"] == "session-07" for r in sess)

    with bench.time("store.list_traces[q=span-name]", limit=500):
        q_hits = store.list_traces(project_id=pid, q="code_review", limit=500)
    # tool "code_review" appears on traces with i % 8 in {3, 6} → 250
    assert len(q_hits) == 250

    with bench.time("store.list_traces[q=trace-id]", limit=50):
        id_hits = store.list_traces(project_id=pid, q=trace_id_of(42), limit=50)
    assert [r["trace_id"] for r in id_hits] == [trace_id_of(42)]

    with bench.time("store.list_traces[tool]", limit=500):
        tool_hits = store.list_traces(project_id=pid, tool="db_query", limit=500)
    assert len(tool_hits) == 250

    with bench.time("store.list_traces[duration-window]", limit=600):
        windowed = store.list_traces(
            project_id=pid,
            min_duration_ns=1_000_000_000,  # 1 000 ms
            max_duration_ns=1_300_000_000,  # 1 300 ms
            limit=600,
        )
    # durations are 900 + (i % 7)*100 ms; residues 1..4 of 1 000 → 572
    assert len(windowed) == 572

    with bench.time("store.list_traces[since+until]", limit=200):
        bounded = store.list_traces(
            project_id=pid,
            since_ns=to_unix_nano(T0 + timedelta(seconds=500 * 10)),
            until_ns=to_unix_nano(T0 + timedelta(seconds=599 * 10)),
            limit=200,
        )
    assert len(bounded) == 100  # traces 500..599 inclusive

    with bench.time("store.list_traces[last-page]", limit=100, offset=900):
        tail = store.list_traces(project_id=pid, limit=100, offset=900)
    assert len(tail) == 100
    assert tail[-1]["trace_id"] == trace_id_of(0)


def test_rollups_sessions_and_tools(bench_store: SeededCorpus, bench: BenchReport) -> None:
    store, pid = bench_store.store, bench_store.project_id

    with bench.time("store.list_sessions", seeded=bench_store.traces):
        sessions = store.list_sessions(project_id=pid, limit=100)
    assert len(sessions) == SESSION_COUNT
    assert all(s["traces"] == bench_store.traces // SESSION_COUNT for s in sessions)
    assert all(s["spans"] == 160 for s in sessions)
    # most-recent activity first: session-24 holds trace 999
    assert sessions[0]["session_id"] == "session-24"

    with bench.time("store.list_tools", seeded=bench_store.spans):
        tools = store.list_tools(project_id=pid, limit=100)
    assert len(tools) == len(TOOL_NAMES)
    assert sum(t["calls"] for t in tools) == bench_store.traces * 2
    assert all(t["calls"] == 250 for t in tools)
    # error tool spans: failing traces (i % 10 == 0) → tool (i % 8) at
    # k=0 → residues {0, 2, 4, 6} of the 100 multiples of 10 → 25 each
    assert sum(t["errors"] for t in tools) == 100
    assert all(t["avg_duration_ms"] == 140.0 for t in tools)


def test_get_trace_point_reads(bench_store: SeededCorpus, bench: BenchReport) -> None:
    store, pid = bench_store.store, bench_store.project_id
    ids = [trace_id_of(i) for i in range(0, bench_store.traces, 20)]  # 50 ids

    with bench.time("store.get_trace[x50]", reads=len(ids)):
        loaded = [store.get_trace(tid, project_id=pid) for tid in ids]

    assert all(t is not None for t in loaded)
    assert all(len(t.spans) == SPANS_PER_TRACE for t in loaded if t is not None)


def test_graph_build(bench: BenchReport) -> None:
    """Graph projection over a 2 000-span tree + a 40-trace session."""
    big = make_tree_trace(2_000)
    with bench.time("graph.build_trace_graph", spans=len(big.spans)):
        graph = build_trace_graph(big)
    assert graph.stats["span_count"] == 2_000
    assert graph.stats["max_depth"] == 12  # 0-indexed heap: longest chain = 12
    assert graph.stats["edge_count"] == len(graph.edges)

    # session-00: i ≡ 0 (mod 25) → traces 0, 25, …, 975 → 40 traces
    session_traces = [make_bench_trace(i) for i in range(0, 1_000, SESSION_COUNT)]
    with bench.time("graph.build_session_graph", traces=len(session_traces)):
        session_graph = build_session_graph("session-00", "bench", session_traces)
    assert len(session_graph.nodes) == 40
    assert len(session_graph.edges) == 39  # pairwise sequence chain


def test_eval_run_battery(bench_store: SeededCorpus, bench: BenchReport) -> None:
    """Fetch 200 stored traces + run the full 12-evaluator registry.

    Mirrors ``POST /v1/eval/run``: per-id ``get_trace`` fan-out, then
    ``evaluate_traces`` — measured as two records so the read cost and
    the evaluator cost stay separable.
    """
    store, pid = bench_store.store, bench_store.project_id
    ids = [r["trace_id"] for r in store.list_traces(project_id=pid, limit=200)]

    t0 = time.perf_counter()
    traces = [t for tid in ids if (t := store.get_trace(tid, project_id=pid))]
    bench.record("eval.fetch_traces", time.perf_counter() - t0, reads=len(ids))
    assert len(traces) == 200

    t0 = time.perf_counter()
    report = evaluate_traces(
        traces,
        EVAL_SPECS,
        dataset="bench-cohort",
        now=datetime(2026, 1, 2, tzinfo=UTC),  # injected — deterministic
    )
    bench.record(
        "eval.battery",
        time.perf_counter() - t0,
        traces=len(traces),
        evaluators=len(EVAL_SPECS),
    )

    assert {s["name"] for s in EVAL_SPECS} == set(EVALUATORS)  # full registry
    assert len(report.trace_ids) == 200
    assert all(len(res) == len(EVAL_SPECS) for res in report.results.values())
    # a battery that scored nothing would be a reporting bug, not a run
    assert report.summary
    assert any(s["n"] > 0 for s in report.summary.values())
