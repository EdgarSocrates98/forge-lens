"""Cycle 03 (graph) + Cycle 04 (cost/waste) unit tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from forge_lens.contracts.v1.trace import (
    IntegrityFlag,
    Span,
    SpanKind,
    SpanLink,
    Trace,
)
from forge_lens.graph import (
    EdgeKind,
    build_session_graph,
    build_trace_graph,
    critical_path,
    self_time_ranking,
)
from forge_lens.intel import PricingRegistry, detect_waste, trace_cost
from forge_lens.intel.pricing import ModelPrice

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TRACE_ID = "a" * 32


def _span(
    sid: str,
    name: str,
    *,
    parent: str | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    start_s: float = 0.0,
    dur_ms: float = 100.0,
    attrs: dict | None = None,
    links: list[SpanLink] | None = None,
    flags: list[IntegrityFlag] | None = None,
) -> Span:
    return Span(
        trace_id=TRACE_ID,
        span_id=sid.ljust(16, "0")[:16],
        parent_span_id=parent.ljust(16, "0")[:16] if parent else None,
        name=name,
        kind=kind,
        start_time=T0 + timedelta(seconds=start_s),
        end_time=T0 + timedelta(seconds=start_s, milliseconds=dur_ms),
        attributes=attrs or {},
        links=links or [],
        integrity_flags=flags or [],
    )


def _trace(spans: list[Span], **kw: object) -> Trace:
    return Trace(
        trace_id=str(kw.get("trace_id", TRACE_ID)),
        project_id="p1",
        session_id=kw.get("session_id"),
        started_at=T0,
        ended_at=T0 + timedelta(seconds=10),
        spans=spans,
    )


def _usage(model: str, inp: int, out: int, **kw: object) -> dict:
    return {
        "forge_lens.token_usage": {
            "model": model,
            "input_tokens": inp,
            "output_tokens": out,
            **kw,
        }
    }


# -- graph ------------------------------------------------------------------


def test_trace_graph_parent_and_sequence_edges() -> None:
    root = _span("10", "root", dur_ms=500)
    a = _span("a1", "first", parent="10", start_s=0.1)
    b = _span("b1", "second", parent="10", start_s=0.3)
    g = build_trace_graph(_trace([root, a, b]))

    kinds = {(e.source, e.target): e.kind for e in g.edges}
    assert kinds[("10".ljust(16, "0"), "a1".ljust(16, "0"))] is EdgeKind.PARENT
    assert kinds[("10".ljust(16, "0"), "b1".ljust(16, "0"))] is EdgeKind.PARENT
    # sibling order: first → second
    assert kinds[("a1".ljust(16, "0"), "b1".ljust(16, "0"))] is EdgeKind.SEQUENCE
    assert g.root_id == "10".ljust(16, "0")
    assert g.stats["span_count"] == 3
    assert g.stats["max_depth"] == 2


def test_trace_graph_missing_parent_and_dangling_link() -> None:
    orphan = _span("e1", "orphan", parent="deadbeef")
    linker = _span(
        "f1",
        "linker",
        links=[SpanLink(trace_id="b" * 32, span_id="f" * 16)],
    )
    g = build_trace_graph(_trace([orphan, linker]))
    link_edges = [e for e in g.edges if e.kind is EdgeKind.LINK]
    assert len(link_edges) == 1
    assert link_edges[0].dangling
    assert g.stats["missing_parents"] == 1
    assert g.stats["dangling_edges"] == 1


def test_critical_path_picks_dominant_chain() -> None:
    root = _span("10", "root", dur_ms=1000)
    fast = _span("a1", "fast-child", parent="10", start_s=0.1, dur_ms=50)
    slow = _span("b1", "slow-child", parent="10", start_s=0.2, dur_ms=900)
    leaf = _span("c1", "leaf", parent="b1", start_s=0.3, dur_ms=400)
    g = build_trace_graph(_trace([root, fast, slow, leaf]))

    cp = critical_path(g)
    assert cp["path"] == [
        "10".ljust(16, "0"),
        "b1".ljust(16, "0"),
        "c1".ljust(16, "0"),
    ]
    assert cp["total_ms"] == 1000 + 900 + 400


def test_self_time_subtracts_children() -> None:
    root = _span("10", "root", dur_ms=500)
    child = _span("c1", "child", parent="10", dur_ms=300)
    g = build_trace_graph(_trace([root, child]))
    rows = {r["id"]: r["self_ms"] for r in self_time_ranking(g)}
    assert rows["10".ljust(16, "0")] == 200.0
    assert rows["c1".ljust(16, "0")] == 300.0


def test_session_graph_sequences_traces() -> None:
    t1 = _trace([], trace_id="1" * 32, session_id="s1")
    t2 = _trace([], trace_id="2" * 32, session_id="s1")
    g = build_session_graph("s1", "p1", [t2, t1])  # unordered input
    assert [n.id for n in g.nodes] == [t1.trace_id, t2.trace_id]
    assert g.edges[0].kind is EdgeKind.SEQUENCE
    assert g.edges[0].source == t1.trace_id


# -- cost -------------------------------------------------------------------


def test_cost_report_exact_pricing_math() -> None:
    reg = PricingRegistry(
        {"test-model": ModelPrice(2.50, 10.00, cached_input_per_mtok=1.25)}
    )
    llm = _span(
        "f1",
        "llm call",
        kind=SpanKind.LLM,
        attrs=_usage("test-model", 1000, 500),
    )
    report = trace_cost(_trace([llm]), reg)
    # 1000x2.50 + 500x10.00 per MTok
    assert report.total_estimated_usd == 0.0075
    assert report.by_model["test-model"]["input_tokens"] == 1000
    assert report.span_costs[0].priced


def test_cost_report_prefix_resolution_and_unpriced() -> None:
    reg = PricingRegistry({"gpt-4o": ModelPrice(2.50, 10.00)})
    dated = _span(
        "f1",
        "llm",
        kind=SpanKind.LLM,
        attrs=_usage("gpt-4o-2024-08-06", 100, 50),
    )
    unknown = _span(
        "22", "llm", kind=SpanKind.LLM, attrs=_usage("mystery-llm", 10, 10), start_s=1
    )
    report = trace_cost(_trace([dated, unknown]), reg)
    assert report.span_costs[0].priced  # prefix-resolved
    assert not report.span_costs[1].priced
    assert report.unpriced_models == ["mystery-llm"]


def test_cost_report_flags_llm_spans_without_usage() -> None:
    bare = _span("f1", "llm", kind=SpanKind.LLM)
    report = trace_cost(_trace([bare]), PricingRegistry())
    assert report.usage_missing_on_llm_spans == [bare.span_id]
    assert report.total_estimated_usd == 0.0


def test_cost_cached_and_reasoning_rates() -> None:
    reg = PricingRegistry(
        {"m": ModelPrice(4.0, 8.0, cached_input_per_mtok=1.0)}
    )
    llm = _span(
        "f1",
        "llm",
        kind=SpanKind.LLM,
        attrs=_usage(
            "m", 1000, 0, cached_input_tokens=400, reasoning_tokens=100
        ),
    )
    report = trace_cost(_trace([llm]), reg)
    # uncached 600x4.0 + cached 400x1.0 + reasoning 100x8.0 (as output)
    expected = (600 * 4.0 + 400 * 1.0 + 100 * 8.0) / 1_000_000
    assert report.total_estimated_usd == round(expected, 8)


# -- waste ------------------------------------------------------------------


def test_waste_retry_amplification() -> None:
    args = {"forge_lens.tool_args": '{"q": "same"}'}
    spans = [
        _span(f"1{i}", "search", kind=SpanKind.TOOL, parent="10",
              start_s=i, attrs=dict(args))
        for i in range(3)
    ]
    findings = detect_waste(_trace(spans), PricingRegistry())
    retry = [f for f in findings if f.kind == "retry_amplification"]
    assert len(retry) == 1
    assert retry[0].evidence["invocations"] == 3


def test_waste_context_replay_and_unknown_tokens() -> None:
    llms = [
        _span(f"2{i}", "llm", kind=SpanKind.LLM, start_s=i,
              attrs=_usage("gpt-4o", 1000 * (i + 1), 10))
        for i in range(3)
    ]
    bare = _span("2e", "llm", kind=SpanKind.LLM, start_s=9)
    findings = detect_waste(
        _trace([*llms, bare]),
        PricingRegistry({"gpt-4o": ModelPrice(2.5, 10.0)}),
    )
    kinds = {f.kind for f in findings}
    assert "context_replay" in kinds
    assert "unknown_tokens" in kinds


def test_waste_duplicate_delivery_priced() -> None:
    dup = _span(
        "d1",
        "llm",
        kind=SpanKind.LLM,
        attrs=_usage("gpt-4o", 1000, 100),
        flags=[IntegrityFlag.DUPLICATE],
    )
    findings = detect_waste(
        _trace([dup]), PricingRegistry({"gpt-4o": ModelPrice(2.5, 10.0)})
    )
    dd = [f for f in findings if f.kind == "duplicate_delivery"]
    assert len(dd) == 1
    assert dd[0].estimated_usd == round((1000 * 2.5 + 100 * 10.0) / 1e6, 8)


def test_waste_unpriced_model_finding() -> None:
    llm = _span("f1", "llm", kind=SpanKind.LLM, attrs=_usage("zzz", 5, 5))
    findings = detect_waste(_trace([llm]), PricingRegistry())
    assert any(f.kind == "unpriced_model" for f in findings)
