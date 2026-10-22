"""Bench plumbing: a session-collected timing report + the seeded corpus.

Surfaces are invoked directly — store, pipeline, graph, eval — never
through uvicorn or sockets, keeping the layer offline and deterministic
in everything except the wall-clock it exists to measure.
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from forge_lens.core.config import CaptureLevel, LensConfig
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.storage.sqlite import SQLiteTraceStore

from .gen import SPANS_PER_TRACE, make_bench_trace, scale_label, scale_project
from .report import BenchReport, SeededCorpus, percentile

SEED_TRACES = 1_000

# Volumetric scales, in traces. At 4 spans/trace these are the 1k / 10k /
# 100k-span corpora the benchmark spec targets. 1M spans is documented
# in docs/benchmarks/ — at the measured ~0.4-1k spans/s pipeline rate it
# blows the ~10 min budget, so it is intentionally not parametrized here.
SCALE_TRACES: tuple[int, ...] = (250, 2_500, 25_000)
SCALE_BATCH_TRACES = 1_000  # 4 000 spans per ingest call — under request caps

_REPORT = BenchReport()


@pytest.fixture(scope="session")
def bench() -> BenchReport:
    """The session report — record timings, never hide them."""
    return _REPORT


@pytest.fixture(scope="session")
def bench_store(
    tmp_path_factory: pytest.TempPathFactory, bench: BenchReport
) -> Iterator[SeededCorpus]:
    """A 1 000-trace store seeded through the real ``IngestPipeline``.

    ``capture=full`` (opt-in) so ``output.value`` survives sanitization
    and the eval battery produces real scores on stored evidence rather
    than exercising only the ``dropped_content`` path.
    """
    db = tmp_path_factory.mktemp("bench-corpus") / "bench.db"
    store = SQLiteTraceStore(db)
    pipeline = IngestPipeline(store, LensConfig(capture=CaptureLevel.FULL))
    traces = [make_bench_trace(i) for i in range(SEED_TRACES)]
    t0 = time.perf_counter()
    result = pipeline.ingest(traces)
    seed_s = time.perf_counter() - t0
    stats = store.stats()
    assert result.accepted_spans == SEED_TRACES * SPANS_PER_TRACE
    assert stats["traces"] == SEED_TRACES
    bench.record(
        "corpus.seed",
        seed_s,
        traces=SEED_TRACES,
        spans=stats["spans"],
        db_bytes=stats["db_size_bytes"],
    )
    yield SeededCorpus(
        store=store,
        project_id="bench",
        traces=SEED_TRACES,
        spans=stats["spans"],
        db_bytes=stats["db_size_bytes"],
    )
    store.close()


@pytest.fixture(
    scope="session",
    params=SCALE_TRACES,
    ids=[scale_label(n) for n in SCALE_TRACES],
)
def scale_corpus(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
    bench: BenchReport,
) -> Iterator[SeededCorpus]:
    """A scale corpus seeded through the real ``IngestPipeline``.

    Same deterministic trace family as ``make_bench_trace`` at N =
    250 / 2 500 / 25 000 traces (1k / 10k / 100k spans). The ingest is
    the measured event: batches of ``SCALE_BATCH_TRACES`` are timed
    individually so the record carries the batch-latency distribution
    (p50/p95/max) next to the total, not just a single throughput number.

    ``capture=full`` so eval-on-stored-traces sees real ``output.value``
    evidence, mirroring ``bench_store``.
    """
    n_traces: int = request.param
    label = scale_label(n_traces)
    project_id = scale_project(n_traces)
    db = tmp_path_factory.mktemp(f"bench-scale-{label}") / "scale.db"
    store = SQLiteTraceStore(db)
    pipeline = IngestPipeline(store, LensConfig(capture=CaptureLevel.FULL))

    batch_s: list[float] = []
    t0 = time.perf_counter()
    for lo in range(0, n_traces, SCALE_BATCH_TRACES):
        hi = min(lo + SCALE_BATCH_TRACES, n_traces)
        batch = [
            make_bench_trace(i, project_id=project_id) for i in range(lo, hi)
        ]
        tb = time.perf_counter()
        result = pipeline.ingest(batch)
        batch_s.append(time.perf_counter() - tb)
        assert result.accepted_spans == (hi - lo) * SPANS_PER_TRACE
    seed_s = time.perf_counter() - t0

    stats = store.stats()
    assert stats["traces"] == n_traces
    bench.record(
        f"scale.ingest.batch[{label}]",
        seed_s,
        traces=n_traces,
        spans=stats["spans"],
        batches=len(batch_s),
        spans_per_s=round(stats["spans"] / seed_s) if seed_s else None,
        batch_ms_p50=round(percentile(batch_s, 50) * 1000, 2),
        batch_ms_p95=round(percentile(batch_s, 95) * 1000, 2),
        batch_ms_max=round(max(batch_s) * 1000, 2),
        db_bytes=stats["db_size_bytes"],
        capture="full",
    )
    yield SeededCorpus(
        store=store,
        project_id=project_id,
        traces=n_traces,
        spans=stats["spans"],
        db_bytes=stats["db_size_bytes"],
    )
    store.close()


# --bench-report is registered in tests/conftest.py — options must be
# added from a conftest loaded during CLI preparse (an ancestor of the
# testpaths arg); this file loads at collection time, which is too late
# for addoption but fine for terminal-summary hooks.


def _env_info() -> dict[str, object]:
    """Best-effort host facts for the report — all fields optional.

    Only populated when the OS exposes them; missing keys just mean the
    platform doesn't publish that file, never a failure.
    """
    info: dict[str, object] = {"cpu_count": os.cpu_count()}
    # getloadavg does not exist on Windows — attribute lookup itself
    # raises AttributeError; OSError covers the exists-but-fails case.
    if hasattr(os, "getloadavg"):
        try:
            la1, la5, la15 = os.getloadavg()
            info["loadavg"] = [round(la1, 2), round(la5, 2), round(la15, 2)]
        except OSError:
            pass
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    info["cpu"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal"):
                    info["mem_total_mb"] = int(line.split()[1]) // 1024
                    break
    except OSError:
        pass
    return info


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if not _REPORT.records:
        return
    terminalreporter.write_sep(
        "=", "forge-lens bench smoke — measured wall time (not SLAs)"
    )
    width = max(len(r.surface) for r in _REPORT.records)
    for r in _REPORT.records:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(r.detail.items()))
        terminalreporter.write_line(
            f"  {r.surface:<{width}}  {r.seconds * 1000:>10.1f} ms  {detail}"
        )
    terminalreporter.write_line(
        "  timings are single-run, hardware-dependent smoke signals"
    )
    path = config.getoption("--bench-report")
    if path:
        payload = {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "env": _env_info(),
            "records": [
                {"surface": r.surface, "seconds": round(r.seconds, 6), **r.detail}
                for r in _REPORT.records
            ],
            "note": "measured run — not a capacity claim; hardware varies",
        }
        Path(path).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        terminalreporter.write_line(f"  report written to {path}")
