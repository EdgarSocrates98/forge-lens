"""Volumetric benchmarks — the spec's 1k / 10k / 100k-span scales.

Complements ``test_bench_smoke`` (correctness smoke on 1k traces) with
scale-out measurement: the same deterministic trace family, the same
surfaces, at corpora of 250 / 2 500 / 25 000 traces. Seeding goes
through the real ``IngestPipeline`` and *is* the ingest measurement —
per-batch times are recorded so percentiles ship next to throughput.

Read surfaces are timed ``READ_REPEATS`` times per scale and the record
carries the median plus every rep — honest spread instead of a single
lucky number. There are NO wall-clock assertions here: correctness of
output only, with closed-form selectivities from the corpus math.
Variance lives in the report, where it belongs.

The WAL test is the concurrency note made executable: three reader
connections hammer ``list_traces``/``get_trace`` on the seeded file
while the writer connection commits 80 single-trace ingests — readers
must see a stable corpus and no ``SQLITE_BUSY`` escapes (busy_timeout),
and both sides' latencies are recorded for the doc table.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import pytest

from forge_lens.core.config import CaptureLevel, LensConfig
from forge_lens.core.time import to_unix_nano
from forge_lens.eval import evaluate_traces
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
    residue_count,
    scale_label,
    tool_trace_hits,
    trace_id_of,
)
from .report import BenchReport, SeededCorpus, median, percentile

pytestmark = pytest.mark.benchmark

SINGLE_PROBES = 100
"""Single-trace ingests timed per scale → p50/p95/p99 per-request latency."""

READ_REPEATS = 3
EVAL_SAMPLE = 200
WAL_READERS = 3
WAL_WRITES = 80

_T = TypeVar("_T")


def _timed_median(
    bench: BenchReport,
    surface: str,
    fn: Callable[[], _T],
    **detail: Any,
) -> _T:
    """Time ``fn`` ``READ_REPEATS`` times; record the median + all reps."""
    samples: list[float] = []
    out: _T | None = None
    for _ in range(READ_REPEATS):
        t0 = time.perf_counter()
        out = fn()
        samples.append(time.perf_counter() - t0)
    bench.record(
        surface,
        median(samples),
        reps_ms=[round(s * 1000, 3) for s in samples],
        **detail,
    )
    return out  # type: ignore[return-value]


def test_scale_ingest_single_trace(scale_corpus: SeededCorpus, bench: BenchReport) -> None:
    """Per-request ingest latency on the populated store.

    ``pipeline.ingest([one_trace])`` x ``SINGLE_PROBES`` — the shape a
    production request actually takes — timed individually against a
    store already holding the full corpus. Probe traces land in a
    sibling project so the seeded corpus keeps its exact counts.
    """
    n, label = scale_corpus.traces, scale_label(scale_corpus.traces)
    pipeline = IngestPipeline(
        scale_corpus.store, LensConfig(capture=CaptureLevel.FULL)
    )
    lat: list[float] = []
    for j in range(SINGLE_PROBES):
        trace = make_bench_trace(n + j, project_id=f"{scale_corpus.project_id}-probe")
        t0 = time.perf_counter()
        result = pipeline.ingest([trace])
        lat.append(time.perf_counter() - t0)
        assert result.accepted_spans == SPANS_PER_TRACE

    bench.record(
        f"scale.ingest.single[{label}]",
        percentile(lat, 50),
        probes=len(lat),
        p50_ms=round(percentile(lat, 50) * 1000, 3),
        p95_ms=round(percentile(lat, 95) * 1000, 3),
        p99_ms=round(percentile(lat, 99) * 1000, 3),
        max_ms=round(max(lat) * 1000, 3),
    )


def test_scale_read_latency(scale_corpus: SeededCorpus, bench: BenchReport) -> None:
    """Every server-side filter at each corpus size (median of 3 reps)."""
    store, pid = scale_corpus.store, scale_corpus.project_id
    n, label = scale_corpus.traces, scale_label(scale_corpus.traces)

    page = _timed_median(
        bench,
        f"scale.list_traces.page[{label}]",
        lambda: store.list_traces(project_id=pid, limit=500),
        seeded=n,
    )
    assert len(page) == min(500, n)
    assert page[0]["trace_id"] == trace_id_of(n - 1)  # newest first

    errors = _timed_median(
        bench,
        f"scale.list_traces.status_error[{label}]",
        lambda: store.list_traces(project_id=pid, status="error", limit=n // 10),
    )
    assert len(errors) == n // 10  # i % 10 == 0 → exactly 10 %

    sess = _timed_median(
        bench,
        f"scale.list_traces.session[{label}]",
        lambda: store.list_traces(
            project_id=pid, session_id="session-07", limit=n // SESSION_COUNT
        ),
    )
    assert len(sess) == n // SESSION_COUNT

    q_expected = tool_trace_hits(n, TOOL_NAMES.index("code_review"))
    q_hits = _timed_median(
        bench,
        f"scale.list_traces.q[{label}]",
        lambda: store.list_traces(project_id=pid, q="code_review", limit=q_expected),
    )
    assert len(q_hits) == q_expected

    tool_expected = tool_trace_hits(n, TOOL_NAMES.index("db_query"))
    tool_hits = _timed_median(
        bench,
        f"scale.list_traces.tool[{label}]",
        lambda: store.list_traces(project_id=pid, tool="db_query", limit=tool_expected),
    )
    assert len(tool_hits) == tool_expected

    dur_expected = residue_count(n, 7, {1, 2, 3, 4})
    windowed = _timed_median(
        bench,
        f"scale.list_traces.duration_window[{label}]",
        lambda: store.list_traces(
            project_id=pid,
            min_duration_ns=1_000_000_000,
            max_duration_ns=1_300_000_000,
            limit=dur_expected,
        ),
    )
    assert len(windowed) == dur_expected

    mid = n // 2
    bounded = _timed_median(
        bench,
        f"scale.list_traces.since_until[{label}]",
        lambda: store.list_traces(
            project_id=pid,
            since_ns=to_unix_nano(T0 + timedelta(seconds=mid * 10)),
            until_ns=to_unix_nano(T0 + timedelta(seconds=(mid + 99) * 10)),
            limit=200,
        ),
    )
    assert len(bounded) == 100  # traces mid..mid+99 inclusive


def test_scale_rollups_and_point_reads(
    scale_corpus: SeededCorpus, bench: BenchReport
) -> None:
    store, pid = scale_corpus.store, scale_corpus.project_id
    n, label = scale_corpus.traces, scale_label(scale_corpus.traces)

    sessions = _timed_median(
        bench,
        f"scale.list_sessions[{label}]",
        lambda: store.list_sessions(project_id=pid, limit=100),
    )
    assert len(sessions) == SESSION_COUNT
    assert all(s["traces"] == n // SESSION_COUNT for s in sessions)

    tools = _timed_median(
        bench,
        f"scale.list_tools[{label}]",
        lambda: store.list_tools(project_id=pid, limit=100),
        spans=scale_corpus.spans,
    )
    assert len(tools) == len(TOOL_NAMES)
    assert sum(t["calls"] for t in tools) == n * 2
    for t in tools:
        assert t["calls"] == tool_trace_hits(n, TOOL_NAMES.index(t["tool"]))

    ids = [trace_id_of(i) for i in range(0, n, max(1, n // 50))][:50]
    reads = _timed_median(
        bench,
        f"scale.get_trace.x50[{label}]",
        lambda: [store.get_trace(tid, project_id=pid) for tid in ids],
        reads=len(ids),
    )
    assert all(t is not None and len(t.spans) == SPANS_PER_TRACE for t in reads)


def test_scale_session_graph(scale_corpus: SeededCorpus, bench: BenchReport) -> None:
    """Session graph at each scale: n/25 traces per session (10→1 000)."""
    n, label = scale_corpus.traces, scale_label(scale_corpus.traces)
    # regenerated, not fetched — the graph's cost is what we're timing
    session_traces = [make_bench_trace(i) for i in range(7, n, SESSION_COUNT)]

    graph = _timed_median(
        bench,
        f"scale.graph.session[{label}]",
        lambda: build_session_graph(
            "session-07", scale_corpus.project_id, session_traces
        ),
        traces=len(session_traces),
    )
    assert len(graph.nodes) == n // SESSION_COUNT
    assert len(graph.edges) == n // SESSION_COUNT - 1


def test_scale_deep_tree_graph(bench: BenchReport) -> None:
    """Trace-graph build on a 10 000-span binary tree (5x the smoke's 2k)."""
    big = make_tree_trace(10_000)
    graph = _timed_median(
        bench,
        "scale.graph.trace_tree[10k-spans]",
        lambda: build_trace_graph(big),
        spans=len(big.spans),
    )
    assert graph.stats["span_count"] == 10_000
    assert graph.stats["edge_count"] == len(graph.edges)


def test_scale_eval(scale_corpus: SeededCorpus, bench: BenchReport) -> None:
    """Eval battery over a fixed 200-trace sample fetched from each scale.

    Fetch cost scales with DB size; evaluator cost is corpus-independent —
    kept as separate records, mirroring ``POST /v1/eval/run``.
    """
    store, pid = scale_corpus.store, scale_corpus.project_id
    n, label = scale_corpus.traces, scale_label(scale_corpus.traces)
    sample = min(EVAL_SAMPLE, n)
    ids = [r["trace_id"] for r in store.list_traces(project_id=pid, limit=sample)]

    t0 = time.perf_counter()
    traces = [t for tid in ids if (t := store.get_trace(tid, project_id=pid))]
    bench.record(f"scale.eval.fetch[{label}]", time.perf_counter() - t0, reads=len(ids))
    assert len(traces) == sample

    t0 = time.perf_counter()
    report = evaluate_traces(
        traces,
        EVAL_SPECS,
        dataset=f"scale-{label}",
        now=datetime(2026, 1, 2, tzinfo=UTC),  # injected — deterministic
    )
    bench.record(
        f"scale.eval.battery[{label}]",
        time.perf_counter() - t0,
        traces=len(traces),
        evaluators=len(EVAL_SPECS),
    )
    assert len(report.trace_ids) == sample
    assert all(len(res) == len(EVAL_SPECS) for res in report.results.values())
    assert any(s["n"] > 0 for s in report.summary.values())


def test_scale_wal_concurrent_readers(
    scale_corpus: SeededCorpus, bench: BenchReport
) -> None:
    """WAL concurrency note: N reader connections during live writes.

    Three reader stores (separate SQLite connections on the same WAL
    file) loop ``list_traces`` + ``get_trace`` while the writer commits
    ``WAL_WRITES`` single-trace ingests. Correctness gate: readers never
    error out (busy_timeout absorbs contention) and always see the
    stable seeded corpus; latencies on both sides are recorded, not
    gated — WAL blocks writers-on-writers, not readers-on-writer.
    """
    store, pid = scale_corpus.store, scale_corpus.project_id
    n, label = scale_corpus.traces, scale_label(scale_corpus.traces)

    # idle read baseline on the writer connection, median of 3
    _timed_median(
        bench,
        f"scale.wal.read_idle[{label}]",
        lambda: store.list_traces(project_id=pid, limit=50),
    )

    readers = [SQLiteTraceStore(store.path) for _ in range(WAL_READERS)]
    stop = threading.Event()
    read_lat: list[float] = []
    errors: list[BaseException] = []
    expected_page = min(50, n)

    def _reader(rstore: SQLiteTraceStore) -> None:
        try:
            while not stop.is_set():
                t0 = time.perf_counter()
                rows = rstore.list_traces(project_id=pid, limit=50)
                read_lat.append(time.perf_counter() - t0)
                if len(rows) != expected_page:
                    errors.append(
                        AssertionError(f"page {len(rows)} != {expected_page}")
                    )
                if rstore.get_trace(trace_id_of(0), project_id=pid) is None:
                    errors.append(AssertionError("get_trace(trace-0) miss"))
        except BaseException as exc:  # surfaced to the assertion below
            errors.append(exc)

    threads = [
        threading.Thread(target=_reader, args=(r,), daemon=True) for r in readers
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)  # let readers get inside their loop before writes

    pipeline = IngestPipeline(store, LensConfig(capture=CaptureLevel.FULL))
    write_lat: list[float] = []
    for j in range(WAL_WRITES):
        trace = make_bench_trace(
            n + SINGLE_PROBES + j, project_id=f"{pid}-walprobe"
        )
        t0 = time.perf_counter()
        pipeline.ingest([trace])
        write_lat.append(time.perf_counter() - t0)

    stop.set()
    for t in threads:
        t.join(timeout=30)
    for r in readers:
        r.close()

    assert not errors, f"reader failures under WAL write load: {errors[:3]}"
    assert len(read_lat) >= WAL_READERS  # every reader got ≥1 read in

    bench.record(
        f"scale.wal.read_during_write[{label}]",
        percentile(read_lat, 50),
        reads=len(read_lat),
        p50_ms=round(percentile(read_lat, 50) * 1000, 3),
        p95_ms=round(percentile(read_lat, 95) * 1000, 3),
        readers=WAL_READERS,
    )
    bench.record(
        f"scale.wal.write_during_reads[{label}]",
        percentile(write_lat, 50),
        writes=len(write_lat),
        p50_ms=round(percentile(write_lat, 50) * 1000, 3),
        p95_ms=round(percentile(write_lat, 95) * 1000, 3),
    )


def test_env_info_survives_platforms_without_getloadavg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """os.getloadavg does not exist on Windows — the attribute lookup
    itself raises AttributeError, so the guard must be hasattr-based,
    not OSError-based."""
    import os

    from .conftest import _env_info

    monkeypatch.delattr(os, "getloadavg", raising=False)
    info = _env_info()
    assert "loadavg" not in info
    assert info["cpu_count"]
