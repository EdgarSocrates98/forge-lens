"""Unit tests for the §06.2/§06.5 evaluators — ``schema_validation``,
``evidence_coverage``, ``human_review``, ``precision_recall`` and
``claims_grounded``. Every verdict is evidence-backed: unobserved
fields/sources yield ``score=None`` with a named reason, malformed
specs degrade to ``evaluator_error``, and ``claims_grounded`` is held
to its documented lexical-overlap semantic (never a factuality claim).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from forge_lens.contracts.v1.evaluation import EvaluationResult
from forge_lens.contracts.v1.trace import Span, SpanKind, Trace
from forge_lens.eval import EVALUATORS, evaluate_traces, run_evaluators

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
TRACE_ID = "b" * 32


def _span(
    sid: str,
    name: str,
    *,
    kind: SpanKind = SpanKind.INTERNAL,
    dur_ms: float = 100.0,
    attrs: dict[str, Any] | None = None,
) -> Span:
    return Span(
        trace_id=TRACE_ID,
        span_id=sid.ljust(16, "0")[:16],
        name=name,
        kind=kind,
        start_time=T0,
        end_time=T0 + timedelta(milliseconds=dur_ms),
        attributes=attrs or {},
    )


def _trace(
    spans: list[Span],
    *,
    ended: bool = True,
    resource_attrs: dict[str, Any] | None = None,
) -> Trace:
    return Trace(
        trace_id=TRACE_ID,
        project_id="p1",
        started_at=T0,
        ended_at=T0 + timedelta(seconds=10) if ended else None,
        spans=spans,
        resource_attributes=resource_attrs or {},
    )


def _run(
    trace: Trace, name: str, params: Any = None
) -> EvaluationResult:
    # same spec path the API exercises — params normalized to {} when absent
    return run_evaluators(trace, [{"name": name, "params": params}])[0]


def _tools_trace() -> Trace:
    return _trace(
        [
            _span("a1", "agent", kind=SpanKind.AGENT),
            _span("c1", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"}),
            _span("c2", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"}),
            _span("c3", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "db_query"}),
            _span("c4", "manual_call", kind=SpanKind.TOOL),  # no tool.name
        ]
    )


# -- registry ------------------------------------------------------------------


def test_wave2_evaluators_registered() -> None:
    for name in (
        "schema_validation",
        "evidence_coverage",
        "human_review",
        "precision_recall",
        "claims_grounded",
    ):
        assert name in EVALUATORS, name
    assert len(EVALUATORS) == 17
    # binary flags: human_review never scores; precision_recall is continuous
    assert EVALUATORS["human_review"][1] is False
    assert EVALUATORS["precision_recall"][1] is False


# -- schema_validation ---------------------------------------------------------

_ORDER_SCHEMA = {
    "type": "object",
    "required": ["name"],
    "properties": {
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "count": {"type": "integer"},
    },
}


def test_schema_validation_passes_on_structured_attr() -> None:
    t = _trace([
        _span("a1", "llm", kind=SpanKind.LLM,
              attrs={"out": {"name": "n1", "tags": ["x", "y"], "count": 2}}),
    ])
    res = _run(t, "schema_validation",
               {"field": "out", "schema": _ORDER_SCHEMA})
    assert res.score == 1.0
    assert res.metric == "field_schema_valid"
    assert res.details["violation_count"] == "0"
    assert res.details["instance_kind"] == "object"
    assert res.details["decoded"] == "raw"
    assert "violations" not in res.details


def test_schema_validation_parses_json_string_output() -> None:
    t = _trace([
        _span("a1", "llm", attrs={"output.value": '{"name": "n1", "tags": []}'})
    ])
    res = _run(t, "schema_validation", {"schema": _ORDER_SCHEMA})
    assert res.score == 1.0
    assert res.details["decoded"] == "json"
    assert res.details["observed_in"] == "a1".ljust(16, "0")


def test_schema_validation_scalar_string_stays_string() -> None:
    """``"42"`` observed as a string is a string — never silently
    re-typed to a number by the JSON-parse convenience path."""
    t = _trace([_span("a1", "llm", attrs={"output.value": "42"})])
    assert _run(t, "schema_validation",
                {"schema": {"type": "string"}}).score == 1.0
    res = _run(t, "schema_validation", {"schema": {"type": "number"}})
    assert res.score == 0.0
    assert "expected number, got string" in res.details["violations"]


def test_schema_validation_reports_violation_paths() -> None:
    t = _trace([
        _span("a1", "llm",
              attrs={"out": {"tags": ["x", 3], "count": 2.5}}),
    ])
    res = _run(t, "schema_validation",
               {"field": "out", "schema": _ORDER_SCHEMA})
    assert res.score == 0.0
    v = res.details["violations"]
    assert "required key 'name' absent" in v
    assert "$.tags[1]: expected string, got integer" in v
    assert "$.count: expected integer, got number" in v
    assert res.details["violation_count"] == "3"


def test_schema_validation_json_schema_integer_semantics() -> None:
    """An integral float (2.0) is an integer under JSON Schema; a bool
    is not a number."""
    t = _trace([_span("a1", "x", attrs={"v": 2.0})])
    assert _run(t, "schema_validation",
                {"field": "v", "schema": {"type": "integer"}}).score == 1.0
    tb = _trace([_span("a1", "x", attrs={"v": True})])
    res = _run(tb, "schema_validation",
               {"field": "v", "schema": {"type": "number"}})
    assert res.score == 0.0
    assert "expected number, got boolean" in res.details["violations"]


def test_schema_validation_unobserved_field_is_none() -> None:
    t = _trace([_span("a1", "x")])
    res = _run(t, "schema_validation", {"schema": {"type": "object"}})
    assert res.score is None
    assert res.details["reason"] == "field not observed in trace"


def test_schema_validation_malformed_schema_is_evaluator_error() -> None:
    t = _trace([_span("a1", "x", attrs={"out": {}})])
    bad: list[dict[str, Any]] = [
        {},                                            # schema missing
        {"schema": "type: object"},                    # not an object
        {"schema": {"type": "object", "minimum": 0}},  # unsupported keyword
        {"schema": {"type": "funky"}},                 # unknown type name
        {"schema": {"type": ["object", "null"]}},      # type unions out of subset
        {"schema": {"required": "name"}},              # required must be a list
        {"schema": {"required": [1, 2]}},              # required must be strings
        {"schema": {"properties": "name"}},            # properties must be object
        {"schema": {"items": "string"}},               # items must be a subschema
    ]
    for params in bad:
        res = _run(t, "schema_validation", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params


def test_schema_validation_deep_nesting_is_evaluator_error() -> None:
    """A schema nested past the recursion bound reports a spec bug —
    never a RecursionError escaping the evaluator."""
    schema: dict[str, Any] = {"type": "object"}
    node = schema
    for _ in range(40):
        node["properties"] = {"a": {}}
        node = node["properties"]["a"]
    t = _trace([_span("a1", "x", attrs={"out": {}})])
    res = _run(t, "schema_validation", {"schema": schema})
    assert res.score is None
    assert res.metric == "evaluator_error"
    assert "nesting" in res.details["reason"]


def test_schema_validation_empty_schema_validates_anything() -> None:
    """``{}`` matches every instance — same as real JSON Schema."""
    t = _trace([_span("a1", "x", attrs={"out": [1, "two", None]})])
    res = _run(t, "schema_validation", {"field": "out", "schema": {}})
    assert res.score == 1.0


# -- evidence_coverage ---------------------------------------------------------


def _evidence_trace() -> Trace:
    return _trace(
        [
            _span("a1", "llm", kind=SpanKind.LLM,
                  attrs={
                      "gen_ai.usage.input_tokens": 10,
                      "forge_lens.token_usage": {"total_tokens": 10},
                  }),
            _span("b1", "call", kind=SpanKind.TOOL,
                  attrs={"tool.name": "web_search"}),
        ],
        resource_attrs={"service.name": "svc"},
    )


def test_evidence_coverage_exact_and_prefix_match() -> None:
    res = _run(
        _evidence_trace(),
        "evidence_coverage",
        {"required_attrs": ["tool.name", "gen_ai.usage", "service.name"]},
    )
    assert res.score == 1.0
    assert res.metric == "required_attrs_coverage"
    assert res.details["coverage"] == "1.000000"
    # "gen_ai.usage" was satisfied by the namespaced key
    assert "gen_ai.usage<-gen_ai.usage.input_tokens" in res.details["evidence"]
    assert res.details["missing"] == ""


def test_evidence_coverage_missing_attrs_fail_with_names() -> None:
    res = _run(
        _evidence_trace(),
        "evidence_coverage",
        {"required_attrs": ["tool.name", "tool.output", "llm.cost"]},
    )
    assert res.score == 0.0
    assert res.details["coverage"].startswith("0.333")
    assert res.details["missing"] == "llm.cost,tool.output"
    assert res.details["covered"] == "tool.name"


def test_evidence_coverage_min_coverage_threshold() -> None:
    t = _evidence_trace()
    params = {"required_attrs": ["tool.name", "tool.output"]}
    # 1 of 2 covered → 0.5: default threshold (1.0) fails it honestly
    assert _run(t, "evidence_coverage", params).score == 0.0
    at = _run(t, "evidence_coverage", {**params, "min_coverage": 0.5})
    assert at.score == 1.0
    assert at.details["coverage"] == "0.500000"
    below = _run(t, "evidence_coverage", {**params, "min_coverage": 0.75})
    assert below.score == 0.0


def test_evidence_coverage_prefix_needs_dot_boundary() -> None:
    """``tool`` must not match the key ``toolshed`` — prefix matching is
    namespaced (``req.``), never a bare startswith."""
    t = _trace([_span("a1", "x", attrs={"toolshed": 1})])
    res = _run(t, "evidence_coverage", {"required_attrs": ["tool"]})
    assert res.score == 0.0
    assert res.details["missing"] == "tool"


def test_evidence_coverage_duplicates_count_once() -> None:
    res = _run(
        _evidence_trace(),
        "evidence_coverage",
        {"required_attrs": ["tool.name", "tool.name", "gen_ai.usage"]},
    )
    assert res.score == 1.0
    assert res.details["required"] == "gen_ai.usage,tool.name"


def test_evidence_coverage_malformed_params_evaluator_error() -> None:
    t = _evidence_trace()
    bad: list[dict[str, Any]] = [
        {},
        {"required_attrs": "tool.name"},            # not a list
        {"required_attrs": []},                     # empty — nothing to check
        {"required_attrs": ["tool.name", 42]},      # non-string key
        {"required_attrs": ["tool.name", ""]},      # empty key
        {"required_attrs": ["tool.name"], "min_coverage": 0},
        {"required_attrs": ["tool.name"], "min_coverage": 1.5},
        {"required_attrs": ["tool.name"], "min_coverage": "0.5"},
        {"required_attrs": ["tool.name"], "min_coverage": True},
        {"required_attrs": ["tool.name"], "min_coverage": float("nan")},
    ]
    for params in bad:
        res = _run(t, "evidence_coverage", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params


# -- human_review ----------------------------------------------------------------


def test_human_review_always_null_never_scores() -> None:
    t = _trace([_span("a1", "x")])
    res = _run(t, "human_review")
    assert res.score is None
    assert res.metric == "pending_human_review"
    assert res.details["reason"] == "pending_human_review"
    assert res.details["verdict"] == "neutral"
    assert res.details["rubric"] == ""


def test_human_review_echoes_rubric_and_counts_as_null() -> None:
    t = _trace([_span("a1", "x")])
    res = _run(t, "human_review",
               {"rubric": "check tone + citations"})
    assert res.score is None
    assert res.details["rubric"] == "check tone + citations"
    # summary: lands in nulls, never silently in means/pass rates
    report = evaluate_traces(
        [t], [{"name": "human_review", "params": {"rubric": "r"}}]
    )
    slot = report.summary["human_review/pending_human_review"]
    assert slot["n"] == 0
    assert slot["nulls"] == 1
    assert slot["mean_score"] is None
    assert slot["pass_rate"] is None


def test_human_review_non_string_rubric_is_evaluator_error() -> None:
    t = _trace([_span("a1", "x")])
    res = _run(t, "human_review", {"rubric": 42})
    assert res.score is None
    assert res.metric == "evaluator_error"


# -- precision_recall ------------------------------------------------------------


def test_precision_recall_tool_mode_perfect_and_partial() -> None:
    t = _tools_trace()
    perfect = _run(
        t, "precision_recall",
        {"tool": True,
         "expected_items": ["web_search", "db_query", "manual_call"]},
    )
    assert perfect.score == 1.0
    assert perfect.details["precision"] == "1.000000"
    assert perfect.details["recall"] == "1.000000"
    assert perfect.details["tp"] == "3"

    partial = _run(
        t, "precision_recall",
        {"tool": True, "expected_items": ["web_search", "db_query"]},
    )
    # observed = {web_search, db_query, manual_call}: tp=2 fp=1 fn=0
    assert partial.score == 0.8
    assert partial.details["precision"].startswith("0.666")
    assert partial.details["recall"] == "1.000000"
    assert partial.details["extra"] == "manual_call"


def test_precision_recall_tool_mode_names_missing_and_extra() -> None:
    t = _tools_trace()
    res = _run(
        t, "precision_recall",
        {"tool": True, "expected_items": ["web_search", "billing_api"]},
    )
    # observed {web_search, db_query, manual_call}: tp=1 fp=2 fn=1
    assert res.details["tp"] == "1"
    assert res.details["fp"] == "2"
    assert res.details["fn"] == "1"
    assert res.details["missing"] == "billing_api"
    assert res.details["extra"] == "db_query,manual_call"
    # p=1/3, r=1/2 → f1 = 0.4
    assert res.score == 0.4


def test_precision_recall_no_tool_spans_is_zero_not_none() -> None:
    """Zero tool calls with non-empty expectations is measurable
    evidence — recall fails for real, not 'unobserved'."""
    t = _trace([_span("a1", "agent", kind=SpanKind.AGENT)])
    res = _run(t, "precision_recall",
               {"tool": True, "expected_items": ["web_search"]})
    assert res.score == 0.0
    assert res.details["fn"] == "1"
    assert "no observed items" in res.details["note"]


def test_precision_recall_field_mode_extraction() -> None:
    # list attribute → element set
    t = _trace([_span("a1", "x", attrs={"ents": ["a", "b", "c"]})])
    res = _run(t, "precision_recall",
               {"field": "ents", "expected_items": ["a", "b"]})
    assert res.score == 0.8  # tp=2 fp=1 fn=0

    # dict attribute → key set
    td = _trace([_span("a1", "x", attrs={"ents": {"x": 1, "y": 2}})])
    resd = _run(td, "precision_recall",
                {"field": "ents", "expected_items": ["x", "z"]})
    assert resd.score == 0.5  # tp=1 fp=1 fn=1 → p=r=0.5

    # JSON-string field → unpacked list
    tj = _trace([_span("a1", "x", attrs={"ents": '["a", "b"]'})])
    resj = _run(tj, "precision_recall",
                {"field": "ents", "expected_items": ["a", "b"]})
    assert resj.score == 1.0

    # scalar → singleton item
    ts = _trace([_span("a1", "x", attrs={"ents": "a"})])
    ress = _run(ts, "precision_recall",
                {"field": "ents", "expected_items": ["a"]})
    assert ress.score == 1.0


def test_precision_recall_field_unobserved_is_none() -> None:
    t = _trace([_span("a1", "x")])
    res = _run(t, "precision_recall",
               {"field": "ents", "expected_items": ["a"]})
    assert res.score is None
    assert res.details["reason"] == "field not observed in trace"


def test_precision_recall_canonical_type_strictness() -> None:
    """``1`` and ``"1"`` are different items — canonical JSON forms keep
    numeric and string expectations distinct."""
    t = _trace([_span("a1", "x", attrs={"ents": [1]})])
    res = _run(t, "precision_recall",
               {"field": "ents", "expected_items": ["1"]})
    assert res.score == 0.0
    assert res.details["missing"] == "1"


def test_precision_recall_malformed_params_evaluator_error() -> None:
    t = _tools_trace()
    bad: list[dict[str, Any]] = [
        {"tool": True},                                  # expected_items missing
        {"tool": True, "expected_items": []},            # empty
        {"tool": True, "expected_items": "web_search"},  # not a list
        {"tool": True, "expected_items": [1, 2]},        # tool mode: strings only
        {"expected_items": ["a"]},                       # no mode declared
        {"tool": True, "field": "ents",
         "expected_items": ["a"]},                       # both modes declared
        {"tool": "yes", "expected_items": ["a"]},        # tool must be bool
        {"field": 7, "expected_items": ["a"]},           # field must be str
    ]
    for params in bad:
        res = _run(t, "precision_recall", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params


# -- claims_grounded -------------------------------------------------------------


def _grounded_trace(output: str, source: Any) -> Trace:
    return _trace(
        [
            _span("a1", "llm", kind=SpanKind.LLM,
                  attrs={"output.value": output}),
            _span("b1", "call", kind=SpanKind.TOOL,
                  attrs={"tool.output": source}),
        ]
    )


def test_claims_grounded_pass_and_fail_by_overlap() -> None:
    t = _grounded_trace(
        "the answer is paris per web results",
        "web results: paris is the capital",
    )
    # claim tokens {the,answer,is,paris,per,web,results}
    # source tokens {web,results,paris,is,the,capital} → 5/7 covered
    res = _run(t, "claims_grounded",
               {"required_source_attr": "tool.output", "min_overlap": 0.5})
    assert res.score == 1.0
    assert res.metric == "claims_source_overlap"
    assert res.details["covered_tokens"] == "5"
    assert res.details["claim_tokens"] == "7"
    assert "not a factuality check" in res.details["heuristic"]

    strict = _run(t, "claims_grounded",
                  {"required_source_attr": "tool.output",
                   "min_overlap": 0.9})
    assert strict.score == 0.0
    assert strict.details["overlap"].startswith("0.714")


def test_claims_grounded_full_overlap_default_threshold() -> None:
    t = _grounded_trace("alpha beta", "alpha beta gamma")
    res = _run(t, "claims_grounded",
               {"required_source_attr": "tool.output"})
    assert res.score == 1.0
    assert res.details["overlap"] == "1.000000"
    assert res.details["min_overlap"] == "0.5"  # documented default


def test_claims_grounded_structured_source_tokenizes_keys_and_values() -> None:
    t = _grounded_trace("temperature is 72f",
                        {"temperature": "72f", "unit": "F"})
    res = _run(t, "claims_grounded",
               {"required_source_attr": "tool.output"})
    # claim {temperature,is,72f} — all three appear in the canonical
    # JSON of the dict value → overlap 1.0
    assert res.score == 1.0


def test_claims_grounded_no_source_evidence_is_none_not_fail() -> None:
    """Absent source evidence can never prove a claim ungrounded —
    the honest verdict is 'not checkable'."""
    t = _trace([_span("a1", "llm", attrs={"output.value": "claims stuff"})])
    res = _run(t, "claims_grounded",
               {"required_source_attr": "tool.output"})
    assert res.score is None
    assert "no source evidence observed" in res.details["reason"]


def test_claims_grounded_claim_unobserved_or_tokenless_is_none() -> None:
    t = _trace([_span("b1", "call", kind=SpanKind.TOOL,
                      attrs={"tool.output": "src"})])
    res = _run(t, "claims_grounded",
               {"required_source_attr": "tool.output"})
    assert res.score is None
    assert res.details["reason"] == "field not observed as string"

    punct = _grounded_trace("!!! ???", "src words")
    res2 = _run(punct, "claims_grounded",
                {"required_source_attr": "tool.output"})
    assert res2.score is None
    assert res2.details["reason"] == "claim carries no checkable tokens"


def test_claims_grounded_malformed_params_evaluator_error() -> None:
    t = _grounded_trace("alpha", "alpha beta")
    bad: list[dict[str, Any]] = [
        {},                                              # required_source_attr
        {"required_source_attr": 42},                    # non-string
        {"required_source_attr": ""},                    # empty
        {"required_source_attr": "tool.output", "min_overlap": 0},
        {"required_source_attr": "tool.output", "min_overlap": 1.2},
        {"required_source_attr": "tool.output", "min_overlap": "high"},
        {"required_source_attr": "tool.output", "min_overlap": False},
    ]
    for params in bad:
        res = _run(t, "claims_grounded", params)
        assert res.score is None, params
        assert res.metric == "evaluator_error", params
