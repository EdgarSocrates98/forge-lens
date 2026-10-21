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


# -- round-10: cyclic + deep + multi-root ------------------------------------


def test_critical_path_survives_parent_cycle() -> None:
    """A↔B parent cycle: must not recurse forever — the closing edge is
    cut deterministically and reported as evidence."""
    a = _span("a1", "A", parent="b1", dur_ms=100)
    b = _span("b1", "B", parent="a1", dur_ms=200, start_s=1)
    g = build_trace_graph(_trace([a, b]))
    cp = critical_path(g)
    assert cp["path"]  # still yields a path
    assert cp["cycles"]  # the cut edge is evidence, not silence
    assert g.stats["cycle_members"] == 2


def test_critical_path_self_parent() -> None:
    s = _span("a1", "self", parent="a1", dur_ms=50)
    g = build_trace_graph(_trace([s]))
    cp = critical_path(g)
    assert cp["total_ms"] == 50
    assert g.stats["cycle_members"] == 1


def test_critical_path_deep_chain_no_recursion() -> None:
    """2000-deep linear chain — plausible for looping agents — must not
    hit the interpreter recursion limit."""
    spans = [
        _span(f"{i:04x}", f"s{i}", parent=(f"{i - 1:04x}" if i else None),
              start_s=i * 0.001, dur_ms=1)
        for i in range(2000)
    ]
    cp = critical_path(build_trace_graph(_trace(spans)))
    assert len(cp["path"]) == 2000
    assert cp["total_ms"] == 2000.0


def test_critical_path_multi_root_takes_max() -> None:
    """Partial traces can arrive with several roots — evaluate all."""
    r1 = _span("a1", "root-a", dur_ms=100)
    r2 = _span("b1", "root-b", dur_ms=500, start_s=1)
    cp = critical_path(build_trace_graph(_trace([r1, r2])))
    assert cp["path"] == ["b1".ljust(16, "0")]
    assert cp["total_ms"] == 500


# -- Cycle 05: failure classification, replay, incident -----------------------


def _err_span(sid: str, name: str, kind: SpanKind, msg: str = "",
              attrs: dict | None = None) -> Span:
    from forge_lens.contracts.v1.trace import Status, StatusCode

    s = _span(sid, name, kind=kind, attrs=attrs)
    s.status = Status(code=StatusCode.ERROR, message=msg or None)
    return s


def test_classify_timeout_and_tool_error() -> None:
    from forge_lens.investigate import classify_failure

    t = _trace([_err_span("a1", "call api", SpanKind.TOOL, msg="ETIMEDOUT")])
    r = classify_failure(t)
    assert r.failed
    assert r.category == "timeout"
    assert r.evidence["span_ids"] == ["a1".ljust(16, "0")]

    t2 = _trace([_err_span("a1", "call api", SpanKind.TOOL, msg="500 boom")])
    r2 = classify_failure(t2)
    assert r2.category == "tool_error"


def test_classify_rate_limit_and_llm() -> None:
    from forge_lens.investigate import classify_failure

    rl = _err_span("a1", "gen", SpanKind.LLM,
                   attrs={"http.status_code": 429})
    assert classify_failure(_trace([rl])).category == "rate_limit"
    bare = _err_span("b1", "gen", SpanKind.LLM, msg="server error")
    assert classify_failure(_trace([bare])).category == "llm_error"


def test_classify_incomplete_and_unknown() -> None:
    from forge_lens.investigate import classify_failure

    hanging = _span("a1", "hanging")
    hanging.end_time = None
    hanging.integrity_flags.append(IntegrityFlag.MISSING_END)
    r = classify_failure(_trace([hanging]))
    assert r.category == "incomplete"
    assert r.confidence == "inferred"

    ok = _span("a1", "fine")
    r2 = classify_failure(_trace([ok]))
    assert not r2.failed
    assert r2.category == "unknown"


def test_replay_timeline_orders_and_offsets() -> None:
    from forge_lens.contracts.v1.trace import SpanEvent
    from forge_lens.investigate import replay_timeline

    s = _span("a1", "work", dur_ms=100)
    s.events.append(SpanEvent(name="mid", time=s.start_time + timedelta(milliseconds=50)))
    out = replay_timeline(_trace([s]))
    kinds = [e["kind"] for e in out["entries"]]
    assert kinds == ["span_start", "event", "span_end"]
    assert out["entries"][0]["offset_ms"] == 0.0
    assert out["entries"][-1]["offset_ms"] == 100.0
    assert out["unclosed_spans"] == 0


def test_incident_bundle_composes_evidence() -> None:
    from forge_lens.intel import PricingRegistry
    from forge_lens.investigate import incident_bundle

    t = _trace([_err_span("a1", "llm", SpanKind.LLM, msg="timeout",
                         attrs=_usage("gpt-4o", 10, 5))])
    b = incident_bundle(t, PricingRegistry())
    assert b["failure"]["category"] == "timeout"
    assert b["graph"]["critical_path"]["path"]
    assert "cost" in b
    assert "waste" in b
    assert "replay" in b
    assert b["bundle_version"] == "1.0.0"


# -- round-11 regressions -----------------------------------------------------


def test_replay_empty_trace_has_full_schema() -> None:
    """Zero-span trace → replay emits every key the CLI/API consume."""
    from forge_lens.investigate import incident_bundle, replay_timeline

    out = replay_timeline(_trace([]))
    assert out["entries"] == []
    assert out["total_ms"] == 0.0
    assert out["unclosed_spans"] == 0
    # the CLI formats bundle['replay']['total_ms'] — must not KeyError
    b = incident_bundle(_trace([]), PricingRegistry())
    assert b["replay"]["total_ms"] == 0.0


def test_classify_cycle_multi_node() -> None:
    """A↔B parent cycle → cycle category with both members as evidence."""
    from forge_lens.investigate import classify_failure

    a = _span("a1", "a", parent="b1")
    b = _span("b1", "b", parent="a1")
    r = classify_failure(_trace([a, b]))
    assert r.failed is True
    assert r.category == "cycle"
    assert sorted(r.evidence["span_ids"]) == [
        "a1".ljust(16, "0"),
        "b1".ljust(16, "0"),
    ]


def test_classify_cycle_self_parent() -> None:
    from forge_lens.investigate import classify_failure

    s = _span("a1", "self", parent="a1")
    r = classify_failure(_trace([s]))
    assert r.category == "cycle"
    assert r.evidence["span_ids"] == ["a1".ljust(16, "0")]


def test_classify_lead_into_cycle_is_not_member() -> None:
    """Node whose chain leads into a cycle is not itself a member."""
    from forge_lens.investigate import classify_failure

    a = _span("a1", "a", parent="b1")
    b = _span("b1", "b", parent="a1")
    lead = _span("c1", "lead", parent="a1")  # hangs off the cycle
    r = classify_failure(_trace([a, b, lead]))
    assert r.category == "cycle"
    assert "c1".ljust(16, "0") not in r.evidence["span_ids"]


def test_classify_clock_skew_is_not_failure() -> None:
    """CLOCK_SKEW is a data-quality flag — not an execution failure."""
    from forge_lens.investigate import classify_failure

    s = _span("a1", "skewed", flags=[IntegrityFlag.CLOCK_SKEW])
    r = classify_failure(_trace([s]))
    assert r.failed is False
    assert r.category == "unknown"


def test_classify_recovered_429_not_failure() -> None:
    """A 429 on a span that ended ok → no rate_limit verdict."""
    from forge_lens.investigate import classify_failure

    ok = _span("a1", "retried", attrs={"http.status_code": 429})
    r = classify_failure(_trace([ok]))
    assert r.failed is False
    assert r.category == "unknown"


def test_classify_context_length_exceeded_not_timeout() -> None:
    from forge_lens.investigate import classify_failure

    s = _err_span("a1", "gen", SpanKind.LLM, msg="context length exceeded")
    r = classify_failure(_trace([s]))
    assert r.category == "llm_error"  # not timeout — "exceeded" alone isn't evidence


def test_classify_timeout_via_exception_type() -> None:
    from forge_lens.investigate import classify_failure

    s = _err_span("a1", "call", SpanKind.TOOL,
                  attrs={"exception.type": "TimeoutError"})
    assert classify_failure(_trace([s])).category == "timeout"


def test_cycle_members_linear_deep_chain() -> None:
    """O(n) walk: 5k-deep chain reports zero cycle members quickly."""
    spans = [
        _span(f"{i:04x}", f"s{i}", parent=f"{i - 1:04x}" if i else None)
        for i in range(5000)
    ]
    g = build_trace_graph(_trace(spans))
    assert g.stats["cycle_members"] == 0
    assert g.stats["span_count"] == 5000


def test_cycle_members_disconnected_cycles() -> None:
    from forge_lens.graph.builder import cycle_member_ids

    p = {
        "a": "b", "b": "a",          # cycle 1
        "c": "d", "d": "e", "e": "c",  # cycle 2 (3 nodes)
        "f": "a",                     # leads into cycle 1 — not a member
        "g": None,                    # clean root
    }
    assert cycle_member_ids(p) == {"a", "b", "c", "d", "e"}


# -- round-12 regressions -----------------------------------------------------


def test_max_depth_covers_all_roots() -> None:
    """Multi-root trace: depth counts every forest, not just the
    elected root's subtree."""
    a = _span("aa", "shallow-root")
    b = _span("bb", "deep-root", start_s=1)
    c = _span("cc", "l2", parent="bb")
    d = _span("dd", "l3", parent="cc")
    e = _span("ee", "l4", parent="dd")
    g = build_trace_graph(_trace([a, b, c, d, e]))
    assert g.stats["max_depth"] == 4


def test_max_depth_includes_cycle_attached_trees() -> None:
    """A tree hanging off a cycle member still contributes depth."""
    a = _span("a1", "a", parent="b1")
    b = _span("b1", "b", parent="a1")
    leaf1 = _span("c1", "child-of-cycle", parent="a1")
    leaf2 = _span("d1", "grandchild", parent="c1")
    g = build_trace_graph(_trace([a, b, leaf1, leaf2]))
    assert g.stats["max_depth"] == 3  # member → child → grandchild
    assert g.stats["cycle_members"] == 2


def test_classify_in_flight_not_failed() -> None:
    """ended_at=None + unclosed spans = running trace, not a failure."""
    from forge_lens.investigate import classify_failure

    s = _span("a1", "running")
    s.end_time = None
    s.integrity_flags.append(IntegrityFlag.MISSING_END)
    t = _trace([s]).model_copy(update={"ended_at": None})
    r = classify_failure(t)
    assert r.failed is False
    assert r.category == "unknown"


def test_classify_truncated_when_envelope_closed() -> None:
    """ended_at set but spans unclosed → host died mid-run."""
    from forge_lens.investigate import classify_failure

    s = _span("a1", "died")
    s.end_time = None
    s.integrity_flags.append(IntegrityFlag.MISSING_END)
    r = classify_failure(_trace([s]))  # _trace sets ended_at
    assert r.failed is True
    assert r.category == "incomplete"
    assert r.confidence == "inferred"


def test_replay_kind_order_at_same_timestamp() -> None:
    """Event stamped at span start: span_start precedes event."""
    from forge_lens.contracts.v1.trace import SpanEvent
    from forge_lens.investigate import replay_timeline

    s = _span("a1", "work", dur_ms=100)
    s.events.append(SpanEvent(name="at-start", time=s.start_time))
    s.events.append(SpanEvent(name="at-end", time=s.end_time))
    out = replay_timeline(_trace([s]))
    kinds = [e["kind"] for e in out["entries"]]
    assert kinds == ["span_start", "event", "event", "span_end"]
    assert out["entries"][1]["event"] == "at-start"


def test_node_has_token_usage_requires_parseable() -> None:
    """Malformed usage dict → has_token_usage False (UNKNOWN_TOKENS bar)."""
    bad = _span(
        "a1", "gen", kind=SpanKind.LLM,
        attrs={"forge_lens.token_usage": {"model": 42, "input_tokens": "x"}},
    )
    g = build_trace_graph(_trace([bad]))
    assert g.nodes[0].has_token_usage is False

    good = _span("b1", "gen2", kind=SpanKind.LLM,
                 attrs=_usage("gpt-4o", 1, 1))
    g2 = build_trace_graph(_trace([good]))
    assert g2.nodes[0].has_token_usage is True
