"""Built-in evaluators — pure functions over a stored Trace.

Every evaluator returns :class:`EvaluationResult` with ``derived``
provenance and the spans/values it used in ``details``. ``binary``
marks pass/fail metrics eligible for the proportion test in
:mod:`forge_lens.eval.compare`; continuous metrics report deltas only.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import Any

from forge_lens.contracts.v1.evaluation import EvaluationResult
from forge_lens.contracts.v1.trace import SpanKind, Trace
from forge_lens.intel.cost import extract_usage

Evaluator = Callable[[Trace, dict[str, Any], datetime | None], EvaluationResult]

_EVALUATOR_VERSION = "1.0.0"

_SPAN_KIND_VALUES = frozenset(k.value for k in SpanKind)
"""Closed ``SpanKind`` value set — ``has_span``'s ``kind`` filter is
validated against it so a typo'd kind is a spec bug, not a silent miss."""


def _result(
    name: str,
    metric: str,
    score: float | None,
    trace: Trace,
    now: datetime | None,
    details: dict[str, str],
) -> EvaluationResult:
    return EvaluationResult(
        evaluator=name,
        evaluator_version=_EVALUATOR_VERSION,
        metric=metric,
        score=score,
        trace_reference=trace.trace_id,
        evaluated_at=now,
        details=details,
    )


def _error_free(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    errs = [s.span_id for s in trace.spans if s.status.code.value == "error"]
    return _result(
        "error_free",
        "no_error_spans",
        0.0 if errs else 1.0,
        trace,
        now,
        {"error_span_ids": ",".join(errs), "error_count": str(len(errs))},
    )


def _latency_budget(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    # `max_duration_ms` is the canonical name; `budget_ms` stays accepted
    # — it is what the CLI (`--budget-ms`) and pre-existing specs emit
    if "max_duration_ms" in params:
        raw = params["max_duration_ms"]
    elif "budget_ms" in params:
        raw = params["budget_ms"]
    else:
        raise KeyError("max_duration_ms")
    # bool is a subclass of int and strings parse through float() — a
    # wire value the caller typed as a number is the only valid shape
    # (same convention as token_budget)
    if isinstance(raw, (bool, str)) or not isinstance(raw, (int, float)):
        raise TypeError(
            f"max_duration_ms must be a number, got {type(raw).__name__}"
        )
    budget = float(raw)
    if not math.isfinite(budget):
        raise ValueError("max_duration_ms must be finite")
    if budget <= 0:
        raise ValueError("max_duration_ms must be positive")
    if trace.ended_at is None:
        return _result(
            "latency_budget",
            "within_budget_ms",
            None,
            trace,
            now,
            {
                "budget_ms": str(budget),
                "reason": "unended trace — duration unknown",
            },
        )
    if not trace.spans:
        return _result(
            "latency_budget",
            "within_budget_ms",
            None,
            trace,
            now,
            {
                "budget_ms": str(budget),
                "reason": "empty trace — duration unknown",
            },
        )
    # envelope wall-clock — the honest "how long did the run take"
    observed = (trace.ended_at - trace.started_at).total_seconds() * 1000.0
    if observed < 0:
        return _result(
            "latency_budget",
            "within_budget_ms",
            None,
            trace,
            now,
            {
                "budget_ms": str(budget),
                "observed_ms": f"{observed:.3f}",
                "reason": "negative envelope duration — no verdict",
            },
        )
    return _result(
        "latency_budget",
        "within_budget_ms",
        1.0 if observed <= budget else 0.0,
        trace,
        now,
        {"observed_ms": f"{observed:.3f}", "budget_ms": str(budget)},
    )


def _token_budget(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    raw_budget = params["max_total_tokens"]
    # bool is a subclass of int — True would silently become budget 1;
    # strings parse fine through int() but a wire string is not a
    # number the caller verified; dicts/lists would fail int() anyway.
    # All are invalid params → no verdict (same convention as
    # latency_budget and _count_bound).
    if isinstance(raw_budget, (bool, str)) or not isinstance(
        raw_budget, (int, float)
    ):
        raise TypeError(
            f"max_total_tokens must be a number, got {type(raw_budget).__name__}"
        )
    if isinstance(raw_budget, float) and (
        not math.isfinite(raw_budget) or not raw_budget.is_integer()
    ):
        # a fractional budget would silently truncate through int() —
        # the same not-a-silent-truncation rule as _count_bound
        raise ValueError("max_total_tokens must be a finite integer")
    budget = int(raw_budget)
    if budget < 0:
        # a negative budget can only ever fail — reporting score 0 for
        # it would masquerade a malformed spec as a trace verdict
        raise ValueError("max_total_tokens must be ≥ 0")
    total = 0
    known = 0
    for s in trace.spans:
        u = extract_usage(s)
        if u is not None:
            known += 1
            # same convention as trace_cost — `or`, not `is not None`:
            # a producer reporting only the aggregate still counts, and a
            # contradictory `total_tokens=0` with nonzero parts counts the
            # parts (over-counting is the safe direction for budgets)
            total += u.total_tokens or (
                (u.input_tokens or 0) + (u.output_tokens or 0)
            )
    unknown = len(trace.spans) - known
    details = {
        "total_tokens": str(total),
        "budget": str(budget),
        "spans_with_usage": str(known),
    }
    score: float | None
    if unknown:
        details["spans_without_usage"] = str(unknown)
        # score on observed tokens; the gap is evidence, not zero
        details["caveat"] = "unobserved spans may add tokens"
    score = 1.0 if total <= budget else 0.0
    return _result("token_budget", "within_token_budget", score, trace, now, details)


def _tool_success(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    if not tools:
        return _result(
            "tool_success", "tool_success_rate", None, trace, now,
            {"reason": "no tool spans in trace"},
        )
    bad = [s.span_id for s in tools if s.status.code.value == "error"]
    rate = (len(tools) - len(bad)) / len(tools)
    return _result(
        "tool_success",
        "tool_success_rate",
        round(rate, 6),
        trace,
        now,
        {
            "tool_spans": str(len(tools)),
            "failed": ",".join(bad),
        },
    )


def _has_span(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    name = params.get("name")
    kind = params.get("kind")
    # malformed filters must not degrade to a silent "span absent" — a
    # non-string name/kind can never match, so it is a spec bug; a kind
    # outside the closed SpanKind enum can never be produced either
    if name is not None and not isinstance(name, str):
        raise TypeError(f"name must be a string, got {type(name).__name__}")
    if kind is not None:
        if not isinstance(kind, str):
            raise TypeError(
                f"kind must be a string, got {type(kind).__name__}"
            )
        if kind not in _SPAN_KIND_VALUES:
            raise ValueError(f"kind must be a known span kind, got {kind!r}")
    hits = [
        s for s in trace.spans
        if (name is None or s.name == name)
        and (kind is None or s.kind.value == kind)
    ]
    return _result(
        "has_span",
        "expected_span_present",
        1.0 if hits else 0.0,
        trace,
        now,
        {
            "name": str(name or "*"),
            "kind": str(kind or "*"),
            "matched": ",".join(s.span_id for s in hits),
        },
    )


def _observed_value(trace: Trace, field: str) -> tuple[Any, str | None, bool]:
    """Resolve ``field`` to the first span attribute carrying it.

    Returns ``(value, span_id, found)``; ``found=False`` when unobserved —
    an absent field is evidence, never an implicit match. ``span_id`` is
    ``None`` for a hit in ``resource_attributes``: a key observed there
    with a JSON ``null`` value still counts as observed (presence is not
    non-nullity), so callers check ``found``, not ``value is None``."""
    for s in trace.spans:
        if field in s.attributes:
            return s.attributes[field], s.span_id, True
    if field in trace.resource_attributes:
        return trace.resource_attributes[field], None, True
    return None, None, False


def _exact_match(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    field = params["field"]  # KeyError → evaluator_error
    if not isinstance(field, str) or not field:
        raise TypeError("field must be a non-empty string")
    expected = params.get("expected")
    observed, span_id, found = _observed_value(trace, field)
    if not found:
        return _result(
            "exact_match",
            "field_exact_match",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed in trace"},
        )
    matched = observed == expected
    return _result(
        "exact_match",
        "field_exact_match",
        1.0 if matched else 0.0,
        trace,
        now,
        {
            "field": field,
            "expected": repr(expected),
            "observed": repr(observed),
            "observed_in": span_id or "resource_attributes",
        },
    )


_WORD_RE = re.compile(r"[a-z0-9]+")


def _token_f1(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Bag-of-tokens F1 between an expected string and an observed field.

    Continuous metric — precision/recall when the expected answer is
    free text; never a binary verdict on approximate match."""
    field = params["field"]  # KeyError → evaluator_error
    if not isinstance(field, str) or not field:
        raise TypeError("token_f1 'field' must be a non-empty string")
    expected = params.get("expected")
    if not isinstance(expected, str):
        raise TypeError("token_f1 'expected' must be a string")
    observed, span_id, _found = _observed_value(trace, field)
    if not isinstance(observed, str):
        return _result(
            "token_f1",
            "field_token_f1",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed as string"},
        )
    exp = Counter(_WORD_RE.findall(expected.lower()))
    obs = Counter(_WORD_RE.findall(observed.lower()))
    if not exp and not obs:
        return _result("token_f1", "field_token_f1", 1.0, trace, now,
                       {"field": field, "reason": "both empty"})
    common = sum((exp & obs).values())
    precision = common / sum(obs.values()) if obs else 0.0
    recall = common / sum(exp.values()) if exp else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    return _result(
        "token_f1",
        "field_token_f1",
        round(f1, 6),
        trace,
        now,
        {
            "field": field,
            "precision": f"{precision:.6f}",
            "recall": f"{recall:.6f}",
            "observed_in": span_id or "resource_attributes",
        },
    )


def _allowed_tools(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Tool-use validation — every TOOL span name ⊆ ``allowed``."""
    allowed = params.get("allowed")
    if not isinstance(allowed, list) or not all(
        isinstance(t, str) for t in allowed
    ):
        raise TypeError("allowed must be a list of tool names")
    allowed_set = set(allowed)
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    # semantic tool name: `tool.name` attribute when observed, else the
    # span name — both are evidence, never guessed
    violations = sorted(
        {
            str(s.attributes.get("tool.name") or s.name)
            for s in tools
            if str(s.attributes.get("tool.name") or s.name) not in allowed_set
        }
    )
    return _result(
        "allowed_tools",
        "tools_within_allowlist",
        1.0 if not violations else 0.0,
        trace,
        now,
        {
            "tool_spans": str(len(tools)),
            "allowed": ",".join(sorted(allowed_set)),
            "violations": ",".join(violations),
        },
    )


def _task_completion(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Task completion — trace finished and (optionally) without error."""
    # non-bool (e.g. the string "false", which is truthy) falls back to
    # the safe default rather than silently inverting the check
    _rne = params.get("require_no_error", True)
    require_no_error = _rne if isinstance(_rne, bool) else True
    if trace.ended_at is None:
        return _result(
            "task_completion",
            "task_completed",
            0.0,
            trace,
            now,
            {"reason": "trace has no end time — still in flight"},
        )
    errors = [s.span_id for s in trace.spans if s.status.code.value == "error"]
    failed = bool(errors) and require_no_error
    return _result(
        "task_completion",
        "task_completed",
        0.0 if failed else 1.0,
        trace,
        now,
        {
            "ended": "true",
            "error_spans": ",".join(errors),
            "require_no_error": str(bool(require_no_error)).lower(),
        },
    )


def _bool_param(params: dict[str, Any], key: str, default: bool) -> bool:
    """Optional bool — a non-bool falls back to ``default`` instead of
    silently flipping semantics (same convention as task_completion)."""
    raw = params.get(key, default)
    return raw if isinstance(raw, bool) else default


def _output_field(params: dict[str, Any]) -> str:
    """Field carrying the trace's final output — ``output.value`` is the
    OpenInference convention; ``field`` overrides for other producers.
    A present-but-malformed override (non-string, empty) is a spec bug —
    ``evaluator_error``, never a silent lookup under a coerced name."""
    raw = params.get("field")
    if raw is None:
        return "output.value"
    if not isinstance(raw, str) or not raw:
        raise TypeError("field must be a non-empty string")
    return raw


def _contains(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Literal-substring check against the trace's observed output."""
    text = params["text"]
    if not isinstance(text, str):
        raise TypeError("contains 'text' must be a string")
    field = _output_field(params)
    case_sensitive = _bool_param(params, "case_sensitive", True)
    observed, span_id, _found = _observed_value(trace, field)
    if not isinstance(observed, str):
        return _result(
            "contains",
            "output_contains",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed as string"},
        )
    haystack = observed if case_sensitive else observed.lower()
    needle = text if case_sensitive else text.lower()
    matched = needle in haystack
    return _result(
        "contains",
        "output_contains",
        1.0 if matched else 0.0,
        trace,
        now,
        {
            "field": field,
            "matched": str(matched).lower(),
            "observed_in": span_id or "resource_attributes",
            "case_sensitive": str(case_sensitive).lower(),
            "needle": text[:120],
        },
    )


def _regex(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Regex search against the trace's observed output — an invalid
    pattern is a malformed spec (evaluator_error), never a crash."""
    pattern = params["pattern"]
    if not isinstance(pattern, str):
        raise TypeError("regex 'pattern' must be a string")
    case_sensitive = _bool_param(params, "case_sensitive", True)
    try:
        rx = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        # re.error is not a ValueError — re-raise as one so the runner's
        # evaluator_error path reports it instead of letting it escape
        raise ValueError(f"invalid pattern: {exc}") from exc
    field = _output_field(params)
    observed, span_id, _found = _observed_value(trace, field)
    if not isinstance(observed, str):
        return _result(
            "regex",
            "output_regex_match",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed as string"},
        )
    m = rx.search(observed)
    details = {
        "field": field,
        "pattern": pattern,
        "matched": str(m is not None).lower(),
        "observed_in": span_id or "resource_attributes",
        "case_sensitive": str(case_sensitive).lower(),
    }
    if m is not None:
        match = m.group(0)
        details["match"] = match[:120] + ("…" if len(match) > 120 else "")
    return _result(
        "regex",
        "output_regex_match",
        1.0 if m is not None else 0.0,
        trace,
        now,
        details,
    )


def _count_bound(params: dict[str, Any], key: str) -> int:
    """``min``/``max`` bound — a non-negative integer; bools, strings and
    fractional floats are invalid params, not silent truncations."""
    raw = params[key]  # KeyError → evaluator_error
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{key} must be an integer, got {type(raw).__name__}")
    if isinstance(raw, float) and (not math.isfinite(raw) or not raw.is_integer()):
        raise ValueError(f"{key} must be a finite integer")
    bound = int(raw)
    if bound < 0:
        raise ValueError(f"{key} must be ≥ 0")
    return bound


def _tool_call_count(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Count TOOL-kind spans (optionally one ``tool.name``) — pass when
    the observed count sits inside the inclusive [min, max] bounds."""
    lo = _count_bound(params, "min")
    hi = _count_bound(params, "max")
    if lo > hi:
        raise ValueError(f"min ({lo}) must be ≤ max ({hi})")
    tool = params.get("tool")
    if tool is not None and (not isinstance(tool, str) or not tool):
        raise TypeError(
            f"tool must be a non-empty string, got {type(tool).__name__}"
        )
    tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
    counted = (
        tools
        if tool is None
        else [s for s in tools if s.attributes.get("tool.name") == tool]
    )
    count = len(counted)
    return _result(
        "tool_call_count",
        "tool_calls_within_bounds",
        1.0 if lo <= count <= hi else 0.0,
        trace,
        now,
        {
            "tool": tool if tool is not None else "*",
            "count": str(count),
            "min": str(lo),
            "max": str(hi),
            "span_ids": ",".join(s.span_id for s in counted),
        },
    )


# -- §06.2/§06.5 evaluators -------------------------------------------------
#
# The evaluation-methods and metrics gaps closed here: output schema
# checking, required-evidence coverage, the human-review queue marker,
# set precision/recall/F1, and a documented lexical-grounding heuristic.
# Same contract as the evaluators above: evidence or ``None``, malformed
# params raise → ``evaluator_error``, collections emitted deterministically.


_JSON_SCHEMA_TYPES = {
    "object",
    "array",
    "string",
    "number",
    "integer",
    "boolean",
    "null",
}
"""Type names the ``schema_validation`` subset accepts."""

_SCHEMA_KEYWORDS = {"type", "properties", "required", "items"}
"""The entire supported JSON-Schema keyword set — anything beyond these
(``minimum``, ``pattern``, ``additionalProperties``, ``enum``, …) is
rejected as a malformed spec rather than silently ignored, which would
fabricate validation the spec author never got."""

_MAX_SCHEMA_DEPTH = 32
"""Recursion bound on nested ``properties``/``items`` — deeper schemas are
a spec bug (evaluator_error), never a ``RecursionError`` in the runner."""

_MAX_VIOLATIONS = 50
"""Detail-string bound — a large array failing ``items`` per element must
not produce a megabyte ``details`` value."""


def _check_schema_node(node: Any, path: str, depth: int) -> None:
    """Validate one schema node against the supported subset.

    Raises ``TypeError``/``ValueError`` — the runner maps both to
    ``evaluator_error``, so a malformed schema is a reported spec bug,
    not a crash and never a silently-relaxed check."""
    if depth > _MAX_SCHEMA_DEPTH:
        raise ValueError(
            f"schema nesting exceeds {_MAX_SCHEMA_DEPTH} levels at {path}"
        )
    if not isinstance(node, dict):
        raise TypeError(f"schema at {path} must be an object")
    unknown = sorted(set(node) - _SCHEMA_KEYWORDS)
    if unknown:
        raise ValueError(
            f"unsupported schema keyword(s) at {path}: {', '.join(unknown)}"
        )
    if "type" in node:
        tname = node["type"]
        if not isinstance(tname, str) or tname not in _JSON_SCHEMA_TYPES:
            raise ValueError(
                f"schema type at {path} must be one of "
                f"{sorted(_JSON_SCHEMA_TYPES)}, got {tname!r}"
            )
    if "required" in node:
        req = node["required"]
        if not isinstance(req, list) or not all(isinstance(k, str) for k in req):
            raise TypeError(f"schema 'required' at {path} must be a list of strings")
    if "properties" in node:
        props = node["properties"]
        if not isinstance(props, dict):
            raise TypeError(f"schema 'properties' at {path} must be an object")
        for key in sorted(props):
            _check_schema_node(props[key], f"{path}.{key}", depth + 1)
    if "items" in node:
        _check_schema_node(node["items"], f"{path}[*]", depth + 1)


def _json_type_name(value: Any) -> str:
    """JSON type name of a Python value — used in violation messages."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        # JSON Schema semantics: 1.0 is an integer
        return "integer" if math.isfinite(value) and value.is_integer() else "number"
    return type(value).__name__


def _schema_type_ok(value: Any, tname: str) -> bool:
    if tname == "object":
        return isinstance(value, dict)
    if tname == "array":
        return isinstance(value, list)
    if tname == "string":
        return isinstance(value, str)
    if tname == "boolean":
        return isinstance(value, bool)
    if tname == "null":
        return value is None
    if tname == "number":
        # bool is a subclass of int — True is not a number; NaN/inf are
        # not representable JSON numbers and honestly fail the check
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    # integer — JSON has a single number type; an integral float counts
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, float) and math.isfinite(value) and value.is_integer()
    )


def _check_instance(
    value: Any, schema: dict[str, Any], path: str, violations: list[str]
) -> None:
    """Apply a validated schema node to an instance, collecting
    ``path: message`` violations. Kind-gated like real JSON Schema:
    ``properties``/``required`` only bind dicts, ``items`` only lists."""
    if len(violations) >= _MAX_VIOLATIONS:
        return
    tname = schema.get("type")
    if tname is not None and not _schema_type_ok(value, tname):
        violations.append(
            f"{path}: expected {tname}, got {_json_type_name(value)}"
        )
        return  # wrong kind — nested checks would only repeat that fact
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                violations.append(f"{path}: required key {key!r} absent")
        props = schema.get("properties", {})
        for key in sorted(props):  # deterministic traversal
            if key in value:
                _check_instance(value[key], props[key], f"{path}.{key}", violations)
    elif isinstance(value, list):
        items = schema.get("items")
        if items is not None:
            for i, element in enumerate(value):
                _check_instance(element, items, f"{path}[{i}]", violations)


def _schema_validation(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Validate the resolved field's JSON value against a schema.

    Supported subset — ``type`` (``object``/``array``/``string``/
    ``number``/``integer``/``boolean``/``null``, single name only),
    ``properties`` (dict of subschemas), ``required`` (list of keys),
    ``items`` (subschema applied to every element). No other keyword is
    accepted — unsupported keys are an ``evaluator_error``, never
    silently skipped. ``type``-less nodes are legal (a ``{}`` schema
    validates anything, as in JSON Schema).

    Field resolution reuses ``_output_field`` (default ``output.value``).
    A string value that parses as a JSON object/array is validated as
    the parsed structure; a string that does not (or parses to a scalar)
    is validated as the string itself — ``"42"`` the string stays a
    string, never a number."""
    schema = params["schema"]  # KeyError → evaluator_error
    _check_schema_node(schema, "$", 0)
    field = _output_field(params)
    observed, span_id, found = _observed_value(trace, field)
    if not found:
        return _result(
            "schema_validation",
            "field_schema_valid",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed in trace"},
        )
    instance = observed
    decoded = "raw"
    if isinstance(observed, str):
        try:
            # RecursionError joins ValueError here: a string nested past
            # the interpreter's parse depth is unparseable input, not a
            # crash — the runner never lets it escape
            parsed: Any = json.loads(observed)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            instance = parsed
            decoded = "json"
    violations: list[str] = []
    _check_instance(instance, schema, "$", violations)
    details = {
        "field": field,
        "observed_in": span_id or "resource_attributes",
        "instance_kind": _json_type_name(instance),
        "decoded": decoded,
        "violation_count": str(len(violations)),
    }
    if violations:
        shown = sorted(violations)
        suffix = (
            f"; … truncated at {_MAX_VIOLATIONS}"
            if len(violations) >= _MAX_VIOLATIONS
            else ""
        )
        details["violations"] = "; ".join(shown) + suffix
    return _result(
        "schema_validation",
        "field_schema_valid",
        1.0 if not violations else 0.0,
        trace,
        now,
        details,
    )


def _fraction_param(params: dict[str, Any], key: str, default: float) -> float:
    """Optional threshold in ``(0, 1]`` — wire strings, bools,
    non-finite and out-of-range values are spec bugs, not clamps."""
    raw = params.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"{key} must be a number, got {type(raw).__name__}")
    value = float(raw)
    if not math.isfinite(value):
        raise ValueError(f"{key} must be finite")
    if not 0.0 < value <= 1.0:
        raise ValueError(f"{key} must be in (0, 1]")
    return value


def _evidence_coverage(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Fraction of ``required_attrs`` present anywhere in the trace.

    Trace-level semantic: an attribute counts as covered when at least
    one span — or ``resource_attributes`` — carries a key equal to it or
    namespaced under it (``gen_ai.usage`` covers
    ``gen_ai.usage.input_tokens``; a bare ``x`` never matches ``xyz``).
    Per-span coverage was rejected: agent evidence legitimately lives
    spread across spans (tool names on TOOL spans, usage on LLM spans),
    so requiring every attribute on one span would fail honest traces.

    ``score`` is the pass verdict against ``min_coverage`` (default 1.0)
    so ``pass_rate`` and the z-test stay truthful; the raw fraction is
    reported in ``details.coverage``."""
    required = params["required_attrs"]  # KeyError → evaluator_error
    if (
        not isinstance(required, list)
        or not required
        or not all(isinstance(a, str) and a for a in required)
    ):
        raise TypeError("required_attrs must be a non-empty list of attribute keys")
    min_cov = _fraction_param(params, "min_coverage", 1.0)
    reqs = sorted(set(required))  # duplicates count once — set semantics
    keys = set(trace.resource_attributes)
    for s in trace.spans:
        keys.update(s.attributes)
    covered: list[str] = []
    missing: list[str] = []
    evidence: list[str] = []
    for req in reqs:
        hits = sorted(k for k in keys if k == req or k.startswith(req + "."))
        if hits:
            covered.append(req)
            evidence.extend(f"{req}<-{k}" for k in hits)
        else:
            missing.append(req)
    coverage = len(covered) / len(reqs)
    ev = ",".join(sorted(evidence))
    details = {
        "required": ",".join(reqs),
        "covered": ",".join(covered),
        "missing": ",".join(missing),
        "coverage": f"{coverage:.6f}",
        "min_coverage": f"{min_cov:g}",
        "attr_keys_observed": str(len(keys)),
        "evidence": ev[:400] + ("…" if len(ev) > 400 else ""),
    }
    return _result(
        "evidence_coverage",
        "required_attrs_coverage",
        1.0 if coverage >= min_cov else 0.0,
        trace,
        now,
        details,
    )


def _human_review(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Human-review queue marker — always ``score=None``.

    A deterministic engine cannot decide a rubric; the result exists to
    mark the trace as pending judgment, never to fabricate one. The
    optional ``rubric`` is echoed verbatim into ``details`` so the
    reviewer sees the instructions the spec carried."""
    rubric = params.get("rubric")
    if rubric is not None and not isinstance(rubric, str):
        raise TypeError(f"rubric must be a string, got {type(rubric).__name__}")
    return _result(
        "human_review",
        "pending_human_review",
        None,
        trace,
        now,
        {
            "reason": "pending_human_review",
            "verdict": "neutral",
            "rubric": rubric or "",
        },
    )


def _canonical_item(item: Any) -> str:
    """Canonical comparison form for expected/observed items — stable
    across dict key order; ``1``/``1.0``/``"1"`` stay distinct."""
    return json.dumps(
        item, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True, default=str,
    )


def _display_item(item: Any) -> str:
    """Human-readable item for details — strings unquoted, the rest canonical."""
    return item if isinstance(item, str) else _canonical_item(item)


def _extract_items(value: Any) -> list[Any]:
    """Observed field value → item list for ``precision_recall``.

    Lists yield their elements; dicts yield their keys; a string that
    parses as a JSON list/dict unpacks the same way; any other value
    (including strings that parse only to JSON scalars) is one item."""
    if isinstance(value, str):
        try:
            # same RecursionError guard as schema_validation — a string
            # nested past the parse depth is unparseable, not a crash
            parsed: Any = json.loads(value)
        except (ValueError, RecursionError):
            parsed = None
        if isinstance(parsed, (list, dict)):
            return _extract_items(parsed)
        return [value]
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, list):
        return list(value)
    return [value]


def _tool_names(trace: Trace) -> set[str]:
    """Semantic tool names actually invoked — ``tool.name`` attribute
    when observed, else the span name (same convention as allowed_tools)."""
    return {
        str(s.attributes.get("tool.name") or s.name)
        for s in trace.spans
        if s.kind is SpanKind.TOOL
    }


def _precision_recall(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Set precision/recall/F1 between observed and ``expected_items``.

    Exactly one observation mode must be declared: ``tool: true``
    compares the set of tool names invoked (``tool.name`` or span name)
    against expected names, or ``field: "<attr>"`` extracts an item set
    from the field's value (see ``_extract_items``). Comparison is on
    canonical JSON forms; duplicates collapse — this is a set metric.

    Score is the F1 (continuous → Mann-Whitney in compare). A field
    that is not observed yields ``score=None``; an observed-but-empty
    item set is real evidence and scores 0.0, as does a trace that
    invoked no tools while ``expected_items`` was non-empty — the
    expectation is spec-side evidence, so the miss is measurable."""
    expected = params["expected_items"]  # KeyError → evaluator_error
    if not isinstance(expected, list) or not expected:
        raise TypeError("expected_items must be a non-empty list")
    tool_sel = params.get("tool", False)
    if not isinstance(tool_sel, bool):
        raise TypeError(f"tool must be a boolean, got {type(tool_sel).__name__}")
    has_field = params.get("field") is not None
    if has_field and (
        not isinstance(params["field"], str) or not params["field"]
    ):
        raise TypeError("field must be a non-empty string")
    if has_field == tool_sel:
        raise ValueError("declare exactly one mode: 'field' or 'tool'")
    observed_items: list[Any]
    details: dict[str, str]
    if tool_sel:
        if not all(isinstance(i, str) for i in expected):
            raise TypeError("tool mode requires expected_items to be strings")
        observed_items = sorted(_tool_names(trace))
        details = {"mode": "tool"}
    else:
        field = str(params["field"])
        observed, span_id, found = _observed_value(trace, field)
        if not found:
            return _result(
                "precision_recall",
                "expected_items_f1",
                None,
                trace,
                now,
                {"field": field, "reason": "field not observed in trace"},
            )
        observed_items = _extract_items(observed)
        details = {
            "mode": "field",
            "field": field,
            "observed_in": span_id or "resource_attributes",
        }
    exp_map = {_canonical_item(i): _display_item(i) for i in expected}
    obs_map = {_canonical_item(i): _display_item(i) for i in observed_items}
    tp = len(set(exp_map) & set(obs_map))
    fp = len(set(obs_map) - set(exp_map))
    fn = len(set(exp_map) - set(obs_map))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    details.update(
        {
            "precision": f"{precision:.6f}",
            "recall": f"{recall:.6f}",
            "tp": str(tp),
            "fp": str(fp),
            "fn": str(fn),
            "missing": ",".join(
                sorted(exp_map[k] for k in set(exp_map) - set(obs_map))
            ),
            "extra": ",".join(
                sorted(obs_map[k] for k in set(obs_map) - set(exp_map))
            ),
        }
    )
    if not obs_map:
        details["note"] = "no observed items — precision/recall degenerate to 0"
    return _result(
        "precision_recall",
        "expected_items_f1",
        round(f1, 6),
        trace,
        now,
        details,
    )


def _claims_grounded(
    trace: Trace, params: dict[str, Any], now: datetime | None
) -> EvaluationResult:
    """Lexical-grounding heuristic for the §06.5 "unsupported claims"
    gap. **Not** a factuality check — this measures what fraction of the
    claim's tokens appear in observed source text, nothing more.

    The claim is the resolved ``field`` (default ``output.value``); the
    sources are every observed value of ``required_source_attr`` across
    spans plus ``resource_attributes`` (e.g. ``tool.output``,
    ``retrieval.documents``). ``overlap = |claim ∩ source tokens| /
    |claim tokens|`` (multiset, same ``_WORD_RE`` convention as
    token_f1); score is the verdict against ``min_overlap`` (default
    0.5) so ``pass_rate`` stays truthful, raw overlap in details.

    Honest limits, also stated in ``details.heuristic``: bag-of-words
    only — no stopword removal, no paraphrase or negation handling, and
    structured source values contribute keys as well as values as
    tokens. When no source evidence is observed the score is ``None``,
    never a fabricated fail — at ``capture=metadata`` content-bearing
    attributes (``retrieval.documents``, ``tool.parameters``…) are
    dropped at ingest, so absence of a source attribute is
    unobservability, not proof the claim was ungrounded."""
    source_attr = params["required_source_attr"]  # KeyError → evaluator_error
    if not isinstance(source_attr, str) or not source_attr:
        raise TypeError("required_source_attr must be a non-empty string")
    min_overlap = _fraction_param(params, "min_overlap", 0.5)
    field = _output_field(params)
    observed, span_id, _found = _observed_value(trace, field)
    if not isinstance(observed, str):
        return _result(
            "claims_grounded",
            "claims_source_overlap",
            None,
            trace,
            now,
            {"field": field, "reason": "field not observed as string"},
        )
    claim_tokens = Counter(_WORD_RE.findall(observed.lower()))
    if not claim_tokens:
        return _result(
            "claims_grounded",
            "claims_source_overlap",
            None,
            trace,
            now,
            {"field": field, "reason": "claim carries no checkable tokens"},
        )
    sources: list[tuple[str, str]] = []
    for s in trace.spans:
        if source_attr in s.attributes:
            value = s.attributes[source_attr]
            sources.append(
                (s.span_id, value if isinstance(value, str) else _canonical_item(value))
            )
    if source_attr in trace.resource_attributes:
        value = trace.resource_attributes[source_attr]
        sources.append(
            (
                "resource_attributes",
                value if isinstance(value, str) else _canonical_item(value),
            )
        )
    if not sources:
        return _result(
            "claims_grounded",
            "claims_source_overlap",
            None,
            trace,
            now,
            {
                "field": field,
                "required_source_attr": source_attr,
                "reason": "no source evidence observed — grounding not checkable",
            },
        )
    src_tokens: Counter[str] = Counter()
    for _sid, text in sources:
        src_tokens.update(_WORD_RE.findall(text.lower()))
    common = sum((claim_tokens & src_tokens).values())
    total = sum(claim_tokens.values())
    overlap = common / total
    return _result(
        "claims_grounded",
        "claims_source_overlap",
        1.0 if overlap >= min_overlap else 0.0,
        trace,
        now,
        {
            "field": field,
            "required_source_attr": source_attr,
            "source_spans": ",".join(sid for sid, _t in sources),
            "claim_tokens": str(total),
            "covered_tokens": str(common),
            "overlap": f"{overlap:.6f}",
            "min_overlap": f"{min_overlap:g}",
            "heuristic": "lexical bag-of-words overlap — not a factuality check",
            "observed_in": span_id or "resource_attributes",
        },
    )


# name → (fn, binary?) — binary marks metrics whose trace-level pass
# fraction (score==1.0 counts) supports the proportion z-test in compare
EVALUATORS: dict[str, tuple[Evaluator, bool]] = {
    "error_free": (_error_free, True),
    "latency_budget": (_latency_budget, True),
    "token_budget": (_token_budget, True),
    "tool_success": (_tool_success, True),
    "has_span": (_has_span, True),
    "exact_match": (_exact_match, True),
    "token_f1": (_token_f1, False),  # continuous — deltas only
    "allowed_tools": (_allowed_tools, True),
    "task_completion": (_task_completion, True),
    "contains": (_contains, True),
    "regex": (_regex, True),
    "tool_call_count": (_tool_call_count, True),
    "schema_validation": (_schema_validation, True),
    "evidence_coverage": (_evidence_coverage, True),
    "human_review": (_human_review, False),  # never scores — queue marker
    "precision_recall": (_precision_recall, False),  # continuous F1
    "claims_grounded": (_claims_grounded, True),
}
"""Binary flag: ``True`` = the score is a per-trace pass (1.0)/fail —
the pass fraction across traces is a proportion the z-test can test."""


def run_evaluators(
    trace: Trace,
    specs: list[Any],
    *,
    now: datetime | None = None,
) -> list[EvaluationResult]:
    """Run named evaluators with params on one trace.

    Unknown evaluator names produce a scored-``None`` result naming the
    miss — an eval run never crashes on a typo'd spec, it reports it.
    ``now`` is injected: the engine never reads a clock.
    """
    out: list[EvaluationResult] = []
    for spec in specs:
        if not isinstance(spec, dict):
            # a malformed spec reports itself — it never 500s the run
            out.append(
                _result(
                    "invalid_spec",
                    "evaluator_error",
                    None,
                    trace,
                    now,
                    {"reason": f"spec must be an object, got {type(spec).__name__}"},
                )
            )
            continue
        name = str(spec.get("name", ""))
        entry = EVALUATORS.get(name)
        params = spec.get("params") or {}
        if entry is None:
            out.append(
                _result(
                    name or "unknown",
                    "unknown_evaluator",
                    None,
                    trace,
                    now,
                    {"reason": f"unknown evaluator {name!r}"},
                )
            )
            continue
        if not isinstance(params, dict):
            out.append(
                _result(
                    name,
                    "evaluator_error",
                    None,
                    trace,
                    now,
                    {"reason": f"params must be an object, got {type(params).__name__}"},
                )
            )
            continue
        fn, _binary = entry
        try:
            out.append(fn(trace, params, now))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            out.append(
                _result(
                    name,
                    "evaluator_error",
                    None,
                    trace,
                    now,
                    {"reason": f"missing/invalid params: {exc}"},
                )
            )
    return out
