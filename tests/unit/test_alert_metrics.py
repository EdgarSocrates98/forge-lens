"""Cycle 13 gap §07.5 — derived alert metrics over the bounded window.

``resolve_metrics`` is exercised against a seeded SQLite store; the
endpoint path is checked once to prove the metrics map reaches
``evaluate_rules`` unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forge_lens.api.app import create_app
from forge_lens.contracts.v1.trace import (
    Span,
    SpanKind,
    Status,
    StatusCode,
    Trace,
)
from forge_lens.core.config import LensConfig
from forge_lens.intel.pricing import PricingRegistry
from forge_lens.ops.alerts import METRIC_WINDOW, METRICS, resolve_metrics
from forge_lens.storage.sqlite import SQLiteTraceStore

_T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _span(
    hexchar: str,
    i: int,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    error: bool = False,
    usage: dict | None = None,
    name: str = "op",
) -> Span:
    attrs: dict = {}
    if usage is not None:
        attrs["forge_lens.token_usage"] = usage
    if kind is SpanKind.TOOL:
        # identical args across calls → detect_loops repeated run
        attrs["tool.args"] = {"path": "/x"}
    return Span(
        trace_id=hexchar * 16,
        span_id=f"{i:016x}",
        name=name,
        kind=kind,
        start_time=_T0 + timedelta(milliseconds=i),
        end_time=_T0 + timedelta(milliseconds=i + 1),
        status=Status(code=StatusCode.ERROR if error else StatusCode.OK),
        attributes=attrs,
    )


def _trace(
    hexchar: str,
    spans: list[Span],
    *,
    duration_ms: int = 100,
    status: StatusCode = StatusCode.OK,
    project_id: str = "default",
    ended: bool = True,
) -> Trace:
    tid = hexchar * 16
    for s in spans:
        object.__setattr__(s, "trace_id", tid)
    return Trace(
        trace_id=tid,
        project_id=project_id,
        root_span_id=spans[0].span_id,
        started_at=_T0,
        ended_at=_T0 + timedelta(milliseconds=duration_ms) if ended else None,
        status=Status(code=status),
        spans=spans,
    )


_PRICED = {"model": "gpt-4o", "input_tokens": 1000, "output_tokens": 500,
           "total_tokens": 1500, "measurement_source": "provider"}
_UNPRICED = {"model": "no-such-model-zz", "input_tokens": 100,
             "output_tokens": 50, "total_tokens": 150,
             "measurement_source": "provider"}


@pytest.fixture
def seeded(tmp_path: Path) -> SQLiteTraceStore:
    """Three default-project traces + one foreign-project trace.

    A: priced LLM usage (gpt-4o: $2.50/$10.00 per MTok → $0.0075),
       100ms, one ok span family.
    B: status error (error span), unpriced usage, 200ms.
    C: 4 identical tool calls → loop finding, 50ms.
    foreign: other project — must not leak into project scope.
    """
    store = SQLiteTraceStore(tmp_path / "m.db")
    store.upsert_trace(_trace("aa", [
        _span("aa", 1, kind=SpanKind.LLM, usage=dict(_PRICED)),
    ], duration_ms=100))
    store.upsert_trace(_trace("bb", [
        _span("bb", 1, kind=SpanKind.LLM, usage=dict(_UNPRICED),
              error=True),
    ], duration_ms=200, status=StatusCode.ERROR))
    store.upsert_trace(_trace("cc", [
        _span("cc", i, kind=SpanKind.TOOL, name="read") for i in range(4)
    ], duration_ms=50))
    store.upsert_trace(_trace("dd", [
        _span("dd", 1, kind=SpanKind.LLM, usage=dict(_PRICED)),
    ], duration_ms=999, project_id="other"))
    yield store
    store.close()


def test_metric_catalog() -> None:
    assert "span_count" in METRICS
    assert "trace_count" in METRICS
    for name in (
        "avg_duration_ms", "max_duration_ms", "p95_duration_ms",
        "error_traces", "error_trace_rate", "token_total",
        "cost_total_usd", "cost_per_trace_usd", "unpriced_model_count",
        "loop_traces",
    ):
        assert name in METRICS, name
    assert "eval_pass_rate" not in METRICS  # not honestly resolvable


def test_store_scope_counters(seeded: SQLiteTraceStore) -> None:
    m = resolve_metrics(seeded, "default", PricingRegistry())
    assert m["trace_count"] == 3.0
    assert m["span_count"] == 6.0
    assert m["error_count"] == 1.0
    assert m["error_rate"] == pytest.approx(1 / 6)


def test_duration_metrics_over_window(seeded: SQLiteTraceStore) -> None:
    m = resolve_metrics(seeded, "default", PricingRegistry())
    assert m["avg_duration_ms"] == pytest.approx((100 + 200 + 50) / 3)
    assert m["max_duration_ms"] == 200.0
    # nearest-rank p95 over [50, 100, 200] → the max
    assert m["p95_duration_ms"] == 200.0


def test_error_trace_metrics_are_trace_level(
    seeded: SQLiteTraceStore,
) -> None:
    m = resolve_metrics(seeded, "default", PricingRegistry())
    assert m["error_traces"] == 1.0
    assert m["error_trace_rate"] == pytest.approx(1 / 3)


def test_token_and_cost_metrics(seeded: SQLiteTraceStore) -> None:
    m = resolve_metrics(seeded, "default", PricingRegistry())
    # priced + unpriced usage both count toward tokens
    assert m["token_total"] == float(1500 + 150)
    # gpt-4o: 1000*2.50/1e6 + 500*10.00/1e6 = 0.0075 — unpriced excluded
    assert m["cost_total_usd"] == pytest.approx(0.0075)
    assert m["cost_per_trace_usd"] == pytest.approx(0.0075 / 3)
    # the excluded model is surfaced, not silently dropped
    assert m["unpriced_model_count"] == 1.0


def test_cost_metrics_absent_without_registry(
    seeded: SQLiteTraceStore,
) -> None:
    m = resolve_metrics(seeded, "default", registry=None)
    assert "cost_total_usd" not in m
    assert "cost_per_trace_usd" not in m
    assert "unpriced_model_count" not in m
    # token accounting needs no pricing — still resolved
    assert m["token_total"] == 1650.0


def test_loop_traces_bounded(seeded: SQLiteTraceStore) -> None:
    m = resolve_metrics(seeded, "default", PricingRegistry())
    assert m["loop_traces"] == 1.0
    # window=1 keeps only the newest trace ("cc" shares _T0 start, so
    # the bound selects ONE row — never a fleet scan)
    m1 = resolve_metrics(seeded, "default", PricingRegistry(), window=1)
    assert m1["trace_count"] == 3.0  # store scope unaffected
    assert m1["loop_traces"] + m1["error_traces"] <= 1.0
    assert m1["token_total"] in (1500.0, 150.0, 0.0)


def test_window_cap_and_empty_store(tmp_path: Path) -> None:
    store = SQLiteTraceStore(tmp_path / "e.db")
    try:
        m = resolve_metrics(store, "default", PricingRegistry())
        assert m["trace_count"] == 0.0
        assert m["avg_duration_ms"] == 0.0
        assert m["error_trace_rate"] == 0.0
        assert m["cost_total_usd"] == 0.0
        assert m["loop_traces"] == 0.0
    finally:
        store.close()


def test_only_skips_trace_scan(seeded: SQLiteTraceStore) -> None:
    m = resolve_metrics(
        seeded, "default", PricingRegistry(), only={"span_count", "bogus"}
    )
    assert m == {"span_count": 6.0}  # unknown names ignored, not error


def test_unended_trace_excluded_from_durations(
    tmp_path: Path,
) -> None:
    store = SQLiteTraceStore(tmp_path / "u.db")
    try:
        store.upsert_trace(_trace("ee", [_span("ee", 1)], duration_ms=40))
        store.upsert_trace(_trace(
            "ff", [_span("ff", 1)], ended=False,
            status=StatusCode.UNSET,
        ))
        m = resolve_metrics(store, "default", PricingRegistry())
        assert m["trace_count"] == 2.0
        assert m["avg_duration_ms"] == 40.0
        assert m["max_duration_ms"] == 40.0
    finally:
        store.close()


def test_metric_window_bound_is_documented() -> None:
    assert METRIC_WINDOW == 200


def test_endpoint_resolves_new_metrics(
    tmp_path: Path, seeded: SQLiteTraceStore,
) -> None:
    cfg = LensConfig(data_dir=tmp_path, db_path=tmp_path / "api.db")
    app = create_app(store=seeded, config=cfg)
    with TestClient(app) as c:
        r = c.post("/v1/alerts/evaluate", json={"rules": [
            {"rule_id": "costy", "metric": "cost_total_usd",
             "op": ">", "threshold": 0.001},
            {"rule_id": "loopy", "metric": "loop_traces",
             "op": ">=", "threshold": 1},
            {"rule_id": "slow", "metric": "p95_duration_ms",
             "op": ">", "threshold": 10000},
            {"rule_id": "err", "metric": "error_trace_rate",
             "op": ">", "threshold": 0.3},
        ]})
        assert r.status_code == 200
        fired = {f["rule_id"] for f in r.json()["fired"]}
        assert fired == {"costy", "loopy", "err"}
