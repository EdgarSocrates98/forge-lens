#!/usr/bin/env python3
"""Forge Lens quickstart — a complete run in ~20 lines, no server needed.

Simulates a small agent (agent → llm + tool), exports the trace straight
into the ingest pipeline + SQLite store, then reads it back — the same
path the CLI and API use.

Run from the repo root:

    uv run python examples/quickstart.py

The database lands in ./.forge-lens/lens.db — inspect it afterwards:

    uv run forge-lens traces list
    uv run forge-lens traces show <trace_id>
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from forge_lens.contracts.v1.trace import Trace, TraceSource
from forge_lens.core.config import LensConfig
from forge_lens.ingest.pipeline import IngestPipeline
from forge_lens.sdk.tracer import LensTracer
from forge_lens.storage.sqlite import SQLiteTraceStore


class LocalPipelineExporter:
    """Export SDK batches straight into the local pipeline (no HTTP).

    Swap this for ``HTTPExporter(endpoint=...)`` to ship to a running
    ``forge-lens serve`` instead.
    """

    def __init__(self, pipeline: IngestPipeline) -> None:
        self._pipeline = pipeline

    def export(self, traces: list[Trace], source: TraceSource | None = None) -> dict:
        result = self._pipeline.ingest(traces)
        return {"accepted_spans": result.accepted_spans}


def main() -> None:
    db_dir = Path(tempfile.gettempdir()) / "forge-lens-quickstart"
    db_path = db_dir / "lens.db"
    store = SQLiteTraceStore(db_path)
    pipeline = IngestPipeline(store, LensConfig(db_path=db_path))
    tracer = LensTracer(
        service_name="quickstart",
        exporter=LocalPipelineExporter(pipeline),
    )

    # --- a small "agent" ---------------------------------------------
    with tracer.agent("research-agent") as agent_span:
        with tracer.llm_call("claude-demo", provider="anthropic") as llm:
            llm.attributes["input.preview"] = "summarize observability"
            tracer.record_usage(llm, input_tokens=120, output_tokens=40)
        with tracer.tool_call("web_search", capture_args=True,
                              arguments={"q": "otel genai semconv"}):
            pass  # imagine a real search here
        agent_span.attributes["demo"] = True

    tracer.shutdown()  # terminal flush — auto-flush already fired at root end

    # --- read it back -------------------------------------------------
    traces = store.list_traces()
    assert traces, "no traces stored"
    trace = store.get_trace(traces[0]["trace_id"])
    assert trace is not None
    print(f"trace {trace.trace_id}  status={trace.status.code.value}  "
          f"spans={len(trace.spans)}")
    for s in sorted(trace.spans, key=lambda x: x.start_time):
        dur = f"{s.duration_ms:.1f}ms" if s.duration_ms is not None else "running"
        print(f"  {s.name:<28} [{s.kind.value:<8}] {dur}")
    print(f"\nstore: {db_path}")
    store.close()


if __name__ == "__main__":
    main()
