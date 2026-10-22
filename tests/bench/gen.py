"""Deterministic synthetic corpus for the bench smoke layer.

Every trace is a pure function of an integer index: fixed base
timestamp, hex-formatted ids, closed-form span layouts. No wall-clock,
no randomness — same index, same trace, on any machine. The shapes
mirror the mini-agent used by the e2e suite (agent → llm + tools) so
timings reflect realistic payloads, not degenerate rows.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
    TraceSource,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)
"""Corpus epoch — arbitrary fixed instant, never ``now()``."""

TOOL_NAMES: tuple[str, ...] = (
    "web_search",
    "read_file",
    "write_file",
    "run_shell",
    "http_fetch",
    "db_query",
    "code_review",
    "summarize",
)
"""Closed palette: every tool gets exactly 250 calls per 1 000 traces."""

SESSION_COUNT = 25
SPANS_PER_TRACE = 4
TOOL_SPANS_PER_TRACE = 2


def scale_label(n_traces: int) -> str:
    """``250`` → ``"1k"`` (spans), matching the published scale names."""
    return f"{n_traces * SPANS_PER_TRACE // 1_000}k"


def scale_project(n_traces: int) -> str:
    return f"scale-{scale_label(n_traces)}"


def residue_count(n: int, mod: int, residues: set[int]) -> int:
    """Count ``i < n`` with ``i % mod in residues`` — closed-form corpus math."""
    return sum(len(range(r, n, mod)) for r in residues)


def tool_trace_hits(n: int, tool_index: int) -> int:
    """Traces (of the first ``n``) whose trace carries ``TOOL_NAMES[tool_index]``.

    Tool span ``k`` is named ``TOOL_NAMES[(i + 3k) % 8]``: k=0 hits when
    ``i ≡ t``, k=1 when ``i ≡ t-3`` — a trace can't hit both (3 ≢ 0 mod 8),
    so span calls and matched-trace counts share this closed form.
    """
    return residue_count(n, len(TOOL_NAMES), {tool_index % 8, (tool_index - 3) % 8})


def trace_id_of(i: int) -> str:
    return f"{i:032x}"


def make_bench_trace(i: int, *, project_id: str = "bench") -> Trace:
    """Trace ``i`` of the synthetic corpus.

    Shape (4 spans): ``agent.run`` root carrying ``output.value``, one
    ``llm`` child with token usage, two ``tool`` children. Deterministic
    distributions, chosen so query benches have known selectivities:

    - ``session_id`` = ``session-{i % 25}`` → 40 traces per session.
    - error traces: ``i % 10 == 0`` → 10 % (tool span 0 fails).
    - envelope duration: ``900 + (i % 7) * 100`` ms → 900-1 500 ms.
    - tool names: ``TOOL_NAMES[(i + 3k) % 8]`` → 250 calls each.
    """
    tid = trace_id_of(i)
    t0 = T0 + timedelta(seconds=i * 10)
    dur_ms = 900 + (i % 7) * 100
    failing = (i % 10) == 0

    root = Span(
        trace_id=tid,
        span_id=f"{1:016x}",
        parent_span_id=None,
        name="agent.run",
        kind=SpanKind.AGENT,
        start_time=t0,
        end_time=t0 + timedelta(milliseconds=dur_ms),
        attributes={"output.value": f"answer {i}: forged result {i % 40}"},
        status=Status(code=StatusCode.ERROR if failing else StatusCode.OK),
    )
    llm = Span(
        trace_id=tid,
        span_id=f"{2:016x}",
        parent_span_id=root.span_id,
        name=f"llm gpt-bench-{i % 3}",
        kind=SpanKind.LLM,
        start_time=t0 + timedelta(milliseconds=50),
        end_time=t0 + timedelta(milliseconds=400),
        attributes={
            "llm.model_name": f"gpt-bench-{i % 3}",
            "forge_lens.token_usage": {
                "provider": "bench",
                "model": f"gpt-bench-{i % 3}",
                "input_tokens": 100 + (i % 50),
                "output_tokens": 40 + (i % 20),
                "total_tokens": 140 + (i % 70),
                "measurement_source": "provider",
            },
        },
        status=Status(code=StatusCode.OK),
    )
    tools = []
    for k in range(TOOL_SPANS_PER_TRACE):
        name = TOOL_NAMES[(i + 3 * k) % len(TOOL_NAMES)]
        bad = failing and k == 0
        tools.append(
            Span(
                trace_id=tid,
                span_id=f"{3 + k:016x}",
                parent_span_id=root.span_id,
                name=f"tool {name}",
                kind=SpanKind.TOOL,
                start_time=t0 + timedelta(milliseconds=420 + k * 200),
                end_time=t0 + timedelta(milliseconds=560 + k * 200),
                attributes={
                    "tool.name": name,
                    "tool.output": f"{name} result for trace {i}",
                },
                status=Status(
                    code=StatusCode.ERROR if bad else StatusCode.OK,
                    message="timeout" if bad else None,
                ),
            )
        )
    return Trace(
        trace_id=tid,
        project_id=project_id,
        session_id=f"session-{i % SESSION_COUNT:02d}",
        root_span_id=root.span_id,
        started_at=t0,
        ended_at=t0 + timedelta(milliseconds=dur_ms),
        status=Status(code=StatusCode.ERROR if failing else StatusCode.OK),
        source=TraceSource(system="forge-lens-bench", sdk_name="bench-gen"),
        resource_attributes={"service.name": "bench-agent"},
        spans=[root, llm, *tools],
    )


def make_tree_trace(n_spans: int, *, project_id: str = "bench-graph") -> Trace:
    """One large trace — a binary-tree span layout (``parent = s // 2``).

    Exercises ``build_trace_graph`` on a shape with real depth (~log2 n)
    and many siblings, not a degenerate chain.
    """
    tid = "f" * 32
    spans = [
        Span(
            trace_id=tid,
            span_id=f"{s:016x}",
            parent_span_id=(f"{s // 2:016x}" if s else None),
            name=f"node-{s % 17}",
            kind=SpanKind.CHAIN,
            start_time=T0 + timedelta(milliseconds=s),
            end_time=T0 + timedelta(milliseconds=s + 5),
            status=Status(code=StatusCode.OK),
        )
        for s in range(n_spans)
    ]
    return Trace(
        trace_id=tid,
        project_id=project_id,
        root_span_id=spans[0].span_id,
        started_at=T0,
        ended_at=T0 + timedelta(milliseconds=n_spans + 5),
        status=Status(code=StatusCode.OK),
        source=TraceSource(system="forge-lens-bench"),
        spans=spans,
    )


EVAL_SPECS: list[dict[str, object]] = [
    {"name": "error_free"},
    {"name": "latency_budget", "params": {"max_duration_ms": 1200}},
    {"name": "token_budget", "params": {"max_total_tokens": 200}},
    {"name": "tool_success"},
    {"name": "has_span", "params": {"kind": "tool"}},
    {
        "name": "exact_match",
        "params": {"field": "output.value", "expected": "answer 0: forged result 0"},
    },
    {
        "name": "token_f1",
        "params": {"field": "output.value", "expected": "forged result"},
    },
    {"name": "allowed_tools", "params": {"allowed": list(TOOL_NAMES)}},
    {"name": "task_completion"},
    {"name": "contains", "params": {"text": "forged", "field": "output.value"}},
    {"name": "regex", "params": {"pattern": r"answer \d+", "field": "output.value"}},
    {"name": "tool_call_count", "params": {"min": 1, "max": 5}},
    {
        "name": "schema_validation",
        "params": {
            "field": "output.value",
            "schema": {"type": "string"},
        },
    },
    {
        "name": "evidence_coverage",
        "params": {"required_attrs": ["tool.name", "forge_lens.token_usage"]},
    },
    {"name": "human_review", "params": {"rubric": "bench placeholder"}},
    {
        "name": "precision_recall",
        "params": {"tool": True, "expected_items": list(TOOL_NAMES)},
    },
    {
        "name": "claims_grounded",
        "params": {"required_source_attr": "tool.output", "min_overlap": 0.2},
    },
]
"""The full 17-evaluator registry, exercised once per stored trace."""
