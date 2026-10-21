"""Bench plumbing: a session-collected timing report + the seeded corpus.

Surfaces are invoked directly — store, pipeline, graph, eval — never
through uvicorn or sockets, keeping the layer offline and deterministic
in everything except the wall-clock it exists to measure.
"""

from __future__ import annotations

import json
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

from .gen import SPANS_PER_TRACE, make_bench_trace
from .report import BenchReport, SeededCorpus

SEED_TRACES = 1_000

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


# --bench-report is registered in tests/conftest.py — options must be
# added from a conftest loaded during CLI preparse (an ancestor of the
# testpaths arg); this file loads at collection time, which is too late
# for addoption but fine for terminal-summary hooks.


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
