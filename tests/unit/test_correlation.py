"""Cycle 13 §05.5 — correlation intelligence tests.

Crafted traces/fleets where each association must compute exactly:
temporal precedence inside a trace, presence-rate co-occurrence across
a bounded window. Honest-empty and insufficient-data paths included.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.intel.correlation import correlate_fleet, correlate_trace
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _span(
    trace_id: str = "0" * 32,
    idx: int = 1,
    name: str = "span",
    *,
    kind: SpanKind = SpanKind.TOOL,
    error: bool = False,
    attrs: dict[str, Any] | None = None,
) -> Span:
    return Span(
        trace_id=trace_id,
        span_id=f"{idx:016x}",
        name=name,
        kind=kind,
        start_time=_T0 + timedelta(milliseconds=idx * 10),
        end_time=_T0 + timedelta(milliseconds=idx * 10 + 5),
        attributes=attrs or {},
        status=Status(code=StatusCode.ERROR if error else StatusCode.OK),
    )


def _usage(model: str, in_toks: int, out_toks: int = 10) -> dict[str, Any]:
    return {
        "forge_lens.token_usage": {
            "model": model,
            "input_tokens": in_toks,
            "output_tokens": out_toks,
            "total_tokens": in_toks + out_toks,
            "measurement_source": "provider",
        }
    }


def _trace(
    idx: int,
    spans: list[Span],
    *,
    status: StatusCode = StatusCode.OK,
    session: str | None = None,
) -> Trace:
    tid = f"{idx:032x}"
    for s in spans:
        s.trace_id = tid
    return Trace(
        trace_id=tid,
        project_id="proj",
        session_id=session,
        root_span_id=spans[0].span_id if spans else None,
        started_at=_T0 + timedelta(seconds=idx),
        ended_at=_T0 + timedelta(seconds=idx, milliseconds=500),
        status=Status(code=status),
        spans=spans,
    )


def _kinds(out: dict[str, Any]) -> set[str]:
    return {c["kind"] for c in out["correlations"]}


def _one(out: dict[str, Any], kind: str) -> dict[str, Any]:
    hits = [c for c in out["correlations"] if c["kind"] == kind]
    assert len(hits) == 1, f"expected exactly one {kind}, got {len(hits)}"
    return hits[0]


# -- per-trace -----------------------------------------------------------


def test_retries_preceded_error_counts_same_name_invocations() -> None:
    tid = "a" * 32
    spans = [
        _span(tid, 1, "web_search", attrs={"tool.name": "web_search"}),
        _span(tid, 2, "web_search", attrs={"tool.name": "web_search"}),
        _span(tid, 3, "web_search", attrs={"tool.name": "web_search"}),
        _span(tid, 4, "web_search", error=True,
              attrs={"tool.name": "web_search"}),
    ]
    out = correlate_trace(_trace(1, spans, status=StatusCode.ERROR))
    c = _one(out, "retries_preceded_error")
    assert c["classification"] == "temporal_association"
    assert c["strength"]["preceding_invocations"] == 3
    assert c["strength"]["error_invocations"] == 1
    prior = next(s for s in c["signals"]
                 if s["type"] == "prior_invocations")
    assert prior["span_ids"] == [
        f"{i:016x}" for i in (1, 2, 3)
    ]
    assert "not proof" in c["caveat"]


def test_no_retries_finding_without_prior_invocations() -> None:
    tid = "b" * 32
    spans = [
        _span(tid, 1, "read"),
        _span(tid, 2, "write", error=True),
    ]
    out = correlate_trace(_trace(1, spans, status=StatusCode.ERROR))
    assert "retries_preceded_error" not in _kinds(out)


def test_tool_failure_cluster_needs_two_errors_same_tool() -> None:
    tid = "c" * 32
    spans = [
        _span(tid, 1, "api.get", error=True,
              attrs={"tool.name": "api.get"}),
        _span(tid, 2, "api.get", error=True,
              attrs={"tool.name": "api.get"}),
        _span(tid, 3, "api.get", attrs={"tool.name": "api.get"}),
        _span(tid, 4, "other", error=True, attrs={"tool.name": "other"}),
    ]
    out = correlate_trace(_trace(1, spans, status=StatusCode.ERROR))
    c = _one(out, "tool_failure_cluster")
    assert c["strength"]["failed_invocations"] == 2
    assert c["strength"]["invocations"] == 3
    assert c["strength"]["failure_share"] == round(2 / 3, 6)


def test_model_switch_flags_distinct_labels() -> None:
    tid = "d" * 32
    spans = [
        _span(tid, 1, "llm a", kind=SpanKind.LLM,
              attrs={"llm.model_name": "model-a"}),
        _span(tid, 2, "llm b", kind=SpanKind.LLM,
              attrs=_usage("model-b", 50)),
        _span(tid, 3, "llm c", kind=SpanKind.LLM),
    ]
    out = correlate_trace(_trace(1, spans))
    c = _one(out, "model_switch")
    assert c["strength"]["distinct_models"] == 2
    assert c["strength"]["calls_without_model"] == 1
    models = {s["value"] for s in c["signals"]}
    assert models == {"model-a", "model-b", None}


def test_single_model_no_switch() -> None:
    tid = "e" * 32
    spans = [
        _span(tid, 1, "llm 1", kind=SpanKind.LLM,
              attrs={"llm.model_name": "same"}),
        _span(tid, 2, "llm 2", kind=SpanKind.LLM,
              attrs={"llm.model_name": "same"}),
    ]
    out = correlate_trace(_trace(1, spans))
    assert "model_switch" not in _kinds(out)
    assert out["signal_inventory"]["models"] == ["same"]


def test_context_growth_preceded_error() -> None:
    tid = "f" * 32
    spans = [
        _span(tid, 1, "llm 1", kind=SpanKind.LLM,
              attrs=_usage("m", 100)),
        _span(tid, 2, "llm 2", kind=SpanKind.LLM,
              attrs=_usage("m", 700)),
        _span(tid, 3, "tool", error=True),
    ]
    out = correlate_trace(_trace(1, spans, status=StatusCode.ERROR))
    c = _one(out, "context_growth_preceded_error")
    assert c["strength"]["input_token_growth"] == 600
    assert c["strength"]["from_input_tokens"] == 100
    assert c["strength"]["to_input_tokens"] == 700
    assert c["strength"]["error_spans_after"] == 1
    ev = next(s for s in c["signals"] if s["type"] == "llm_calls")
    assert ev["span_ids"] == [f"{1:016x}", f"{2:016x}"]


def test_context_growth_preceded_eval_and_errors_before_eval() -> None:
    tid = "a1" * 16
    spans = [
        _span(tid, 1, "llm 1", kind=SpanKind.LLM,
              attrs=_usage("m", 100)),
        _span(tid, 2, "llm 2", kind=SpanKind.LLM,
              attrs=_usage("m", 400)),
        _span(tid, 3, "tool", error=True),
        _span(tid, 4, "judge", kind=SpanKind.EVALUATOR,
              attrs={"eval.score": 0.4}),
    ]
    out = correlate_trace(_trace(1, spans, status=StatusCode.ERROR))
    c = _one(out, "context_growth_preceded_eval")
    assert c["strength"]["input_token_growth"] == 300
    assert c["strength"]["min_score"] == 0.4
    c2 = _one(out, "errors_preceded_eval_score")
    assert c2["strength"]["error_spans_before"] == 1
    assert c2["strength"]["min_score"] == 0.4


def test_clean_trace_returns_honest_empty() -> None:
    tid = "ab" * 16
    spans = [
        _span(tid, 1, "llm", kind=SpanKind.LLM,
              attrs=_usage("m", 100)),
        _span(tid, 2, "tool"),
    ]
    out = correlate_trace(_trace(1, spans))
    assert out["correlations"] == []
    inv = out["signal_inventory"]
    assert inv["llm_spans"] == 1
    assert inv["tool_spans"] == 1
    assert inv["error_spans"] == 0
    assert inv["eval_scores_observed"] == 0


def test_correlate_trace_deterministic() -> None:
    tid = "ac" * 16
    spans = [
        _span(tid, 1, "x", attrs={"tool.name": "x"}),
        _span(tid, 2, "x", error=True, attrs={"tool.name": "x"}),
        _span(tid, 3, "judge", kind=SpanKind.EVALUATOR,
              attrs={"eval.score": 0.2}),
    ]
    t = _trace(1, spans, status=StatusCode.ERROR)
    assert correlate_trace(t) == correlate_trace(t)


# -- fleet ---------------------------------------------------------------


def _seed_fleet(
    store: SQLiteTraceStore,
    *,
    n_failed: int,
    n_ok: int,
    failed_tools: tuple[str, ...] = ("web_search",),
    ok_tools: tuple[str, ...] = ("read",),
    sessions: bool = False,
) -> None:
    """Deterministic two-cohort fleet: failed traces carry
    ``failed_tools``, ok traces carry ``ok_tools``."""
    idx = 0
    for _ in range(n_failed):
        idx += 1
        spans = [
            _span("0" * 32, j + 1, name, attrs={"tool.name": name})
            for j, name in enumerate(failed_tools)
        ]
        spans.append(
            _span("0" * 32, len(spans) + 1, "boom", error=True,
                  attrs={"tool.name": "web_search"})
        )
        store.upsert_trace(_trace(
            idx, spans, status=StatusCode.ERROR,
            session="sess-f" if sessions else None,
        ))
    for i in range(n_ok):
        idx += 1
        spans = [
            _span("0" * 32, j + 1, name, attrs={"tool.name": name})
            for j, name in enumerate(ok_tools)
        ]
        store.upsert_trace(_trace(
            idx, spans, status=StatusCode.OK,
            session=f"sess-ok-{i}" if sessions else None,
        ))


def test_fleet_empty_store_is_honest(tmp_store: SQLiteTraceStore) -> None:
    out = correlate_fleet(tmp_store, "proj")
    assert out["window"]["traces"] == 0
    assert out["outcomes"] == {"failed": 0, "ok": 0, "other": 0}
    for table in out["tables"].values():
        assert isinstance(table, dict)
    assert out["tables"]["tool_error"]["rows"] == []
    assert out["trace_signals"] == []
    assert out["classification"] == "descriptive_co_occurrence"


def test_fleet_tool_error_rates_compute(tmp_store: SQLiteTraceStore) -> None:
    _seed_fleet(tmp_store, n_failed=6, n_ok=6, sessions=True)
    out = correlate_fleet(tmp_store, "proj")
    tbl = out["tables"]["tool_error"]
    rows = {r["tool"]: r for r in tbl["rows"]}
    ws = rows["web_search"]
    assert ws["rate_failed"] == 1.0
    assert ws["rate_ok"] == 0.0
    assert ws["rate_difference_pp"] == 100.0
    assert ws["sufficient"] is True
    assert ws["n"]["failed_with"] == 6
    assert ws["n"]["ok_with"] == 0
    read = rows["read"]
    assert read["rate_failed"] == 0.0
    assert read["rate_ok"] == 1.0
    assert read["rate_difference_pp"] == -100.0
    # rows sorted by rate_difference desc — web_search leads
    assert tbl["rows"][0]["tool"] == "web_search"


def test_fleet_small_cohort_is_insufficient(
    tmp_store: SQLiteTraceStore,
) -> None:
    _seed_fleet(tmp_store, n_failed=3, n_ok=4)
    out = correlate_fleet(tmp_store, "proj")
    tbl = out["tables"]["tool_error"]
    assert tbl["classification"] == "insufficient_data"
    ws = next(r for r in tbl["rows"] if r["tool"] == "web_search")
    assert ws["sufficient"] is False
    assert ws["classification"] == "insufficient_data"
    assert "reason" in ws
    # numbers still shown — insufficiency is labelled, not hidden
    assert ws["rate_failed"] == 1.0


def test_fleet_session_length_buckets(tmp_store: SQLiteTraceStore) -> None:
    _seed_fleet(tmp_store, n_failed=6, n_ok=6, sessions=True)
    out = correlate_fleet(tmp_store, "proj")
    tbl = out["tables"]["session_length_error"]
    rows = {r["bucket"]: r for r in tbl["rows"]}
    # 6 failed traces share one session → "5+"; 6 ok are solo → "1"
    assert rows["5+"]["traces"] == 6
    assert rows["5+"]["failure_rate"] == 1.0
    assert rows["1"]["traces"] == 6
    assert rows["1"]["failure_rate"] == 0.0
    assert tbl["traces_without_session"] == 0


def test_fleet_retry_eval_buckets(tmp_store: SQLiteTraceStore) -> None:
    idx = 0
    # 3 scored traces with repeated invocations → bucket "1-2"
    for _ in range(3):
        idx += 1
        spans = [
            _span("0" * 32, 1, "poll", attrs={"tool.name": "poll"}),
            _span("0" * 32, 2, "poll", attrs={"tool.name": "poll"}),
            _span("0" * 32, 3, "judge", kind=SpanKind.EVALUATOR,
                  attrs={"eval.score": 0.5}),
        ]
        tmp_store.upsert_trace(_trace(idx, spans))
    # 3 scored traces with zero retry signals → bucket "0"
    for _ in range(3):
        idx += 1
        spans = [
            _span("0" * 32, 1, "once", attrs={"tool.name": "once"}),
            _span("0" * 32, 2, "judge", kind=SpanKind.EVALUATOR,
                  attrs={"eval.score": 1.0}),
        ]
        tmp_store.upsert_trace(_trace(idx, spans))
    out = correlate_fleet(tmp_store, "proj")
    tbl = out["tables"]["retry_eval"]
    rows = {r["bucket"]: r for r in tbl["rows"]}
    assert rows["0"]["traces"] == 3
    assert rows["0"]["scored_traces"] == 3
    assert rows["0"]["mean_score"] == 1.0
    assert rows["0"]["sufficient"] is True
    assert rows["1-2"]["traces"] == 3
    assert rows["1-2"]["scored_traces"] == 3
    assert rows["1-2"]["mean_score"] == 0.5
    assert rows["1-2"]["score_delta_vs_window"] == round(
        0.5 - 0.75, 6
    )
    assert rows["3+"]["traces"] == 0
    assert tbl["traces_scored"] == 6


def test_fleet_retry_eval_insufficient_when_unscored(
    tmp_store: SQLiteTraceStore,
) -> None:
    spans = [_span("0" * 32, 1, "t")]
    tmp_store.upsert_trace(_trace(1, list(spans)))
    out = correlate_fleet(tmp_store, "proj")
    row = out["tables"]["retry_eval"]["rows"][0]
    assert row["bucket"] == "0"
    assert row["sufficient"] is False
    assert row["mean_score"] is None


def test_fleet_window_bounded_and_deterministic(
    tmp_store: SQLiteTraceStore,
) -> None:
    _seed_fleet(tmp_store, n_failed=6, n_ok=6)
    out = correlate_fleet(tmp_store, "proj", limit=3)
    assert out["window"]["limit_applied"] == 3
    assert out["window"]["traces"] == 3
    out_big = correlate_fleet(tmp_store, "proj", limit=9999)
    assert out_big["window"]["limit_applied"] == 500
    assert correlate_fleet(tmp_store, "proj") == correlate_fleet(
        tmp_store, "proj"
    )
