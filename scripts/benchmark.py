#!/usr/bin/env python3
"""Cycle 09 benchmark — real ingest+query timings, never fabricated.

Runs a synthetic trace generator through the SQLite store at each
requested scale and records wall time. Results go to stdout as JSON;
paste into docs/benchmarks/README.md with the machine specs printed.

Usage: python scripts/benchmark.py --scale 1000 10000
"""
from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _gen(n_spans: int, *, spans_per_trace: int = 10) -> list[Trace]:
    traces = []
    for t in range(max(1, n_spans // spans_per_trace)):
        tid = f"{t:032x}"
        spans = [
            Span(
                trace_id=tid, span_id=f"{s:016x}", name=f"span-{s % 7}",
                kind=SpanKind.TOOL if s % 3 else SpanKind.LLM,
                start_time=_T0 + timedelta(milliseconds=s),
                end_time=_T0 + timedelta(milliseconds=s + 5),
                status=Status(code=StatusCode.OK),
                attributes={"i": s},
            )
            for s in range(spans_per_trace)
        ]
        traces.append(Trace(
            trace_id=tid, root_span_id=spans[0].span_id, spans=spans,
            started_at=_T0, ended_at=_T0 + timedelta(milliseconds=100),
            status=Status(code=StatusCode.OK),
        ))
    return traces


def _bench(n_spans: int, tmp: Path) -> dict:
    db = tmp / f"bench-{n_spans}.db"
    store = SQLiteTraceStore(db)
    traces = _gen(n_spans)
    t0 = time.perf_counter()
    for tr in traces:
        store.upsert_trace(tr)
    ingest_s = time.perf_counter() - t0
    t0 = time.perf_counter()
    _ = store.list_traces(limit=500)
    query_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    _ = store.get_trace(traces[-1].trace_id)
    get_ms = (time.perf_counter() - t0) * 1000
    size = db.stat().st_size
    store.close()
    return {
        "spans_requested": n_spans,
        "traces": len(traces),
        "ingest_seconds": round(ingest_s, 3),
        "spans_per_second": round(len(traces) * len(traces[0].spans) / ingest_s)
        if ingest_s else None,
        "list_500_ms": round(query_ms, 2),
        "get_trace_ms": round(get_ms, 2),
        "db_bytes": size,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, nargs="+", default=[1000, 10000])
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as td:
        results = [_bench(n, Path(td)) for n in sorted(args.scale)]
    print(json.dumps({
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "results": results,
        "note": "real measured run — not a capacity claim; hardware varies",
    }, indent=2))


if __name__ == "__main__":
    main()
