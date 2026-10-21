"""Bounded specialist-analysis runtime — Cycle 07 §07.1/§07.2.

Specialists are deterministic analyzers over stored evidence, run under
explicit budgets (iterations, tokens, wall-time via an injectable
clock) with cancellation. No LLM calls — a bounded orchestrator, not a
magic box, per the prompt's own rule ("não utilizar agentes para
operações que uma regra determinística resolve melhor").
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from forge_lens.contracts.v1.trace import Trace
from forge_lens.graph import build_trace_graph, critical_path
from forge_lens.investigate import classify_failure
from forge_lens.ops.loops import detect_loops

ToolFn = Callable[[Trace, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class AgentBudget:
    max_iterations: int = 8
    token_budget: int = 200_000
    timeout_s: float = 30.0


@dataclass
class AgentRun:
    run_id: str
    specialist: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0
    tokens_observed: int = 0
    cancelled: bool = False
    stop_reason: str = "completed"
    _cancel: bool = field(default=False, repr=False)

    def cancel(self) -> None:
        self._cancel = True


def _registry() -> dict[str, ToolFn]:
    def _trace_investigator(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"summary": f"{len(trace.spans)} spans", "status": trace.status.code.value}

    def _loop_specialist(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        return detect_loops(trace, repeat_threshold=int(ctx.get("repeat_threshold", 3)))

    def _tool_analyst(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        from forge_lens.contracts.v1.trace import SpanKind

        tools = [s for s in trace.spans if s.kind is SpanKind.TOOL]
        errs = [s.span_id for s in tools if s.status.code.value == "error"]
        return {"tool_spans": len(tools), "failed": errs}

    def _cost_analyst(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        from forge_lens.intel.cost import trace_cost
        from forge_lens.intel.pricing import PricingRegistry

        reg = ctx.get("pricing")
        if not isinstance(reg, PricingRegistry):
            return {"reason": "no pricing registry"}
        return trace_cost(trace, reg).model_dump(mode="json")

    def _context_analyst(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        # cumulative input tokens across LLM spans = context-growth
        # evidence; deterministic, no pricing needed
        from forge_lens.intel.cost import extract_usage

        totals: list[int] = []
        running = 0
        for span in sorted(trace.spans, key=lambda s: (s.start_time, s.span_id)):
            u = extract_usage(span)
            if u is not None:
                running += u.input_tokens or 0
                totals.append(running)
        growth = (totals[-1] - totals[0]) if len(totals) > 1 else 0
        return {"llm_spans": len(totals), "cumulative_input_tokens": running,
                "input_token_growth": growth}

    def _failure_investigator(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        return classify_failure(trace).model_dump(mode="json")

    def _graph_analyst(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        return critical_path(build_trace_graph(trace))

    def _evidence_validator(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        missing = [
            s.span_id for s in trace.spans if not s.name or s.start_time is None
        ]
        return {"invalid_evidence_spans": missing}

    def _eval_analyst(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        from forge_lens.eval.evaluators import run_evaluators

        res = run_evaluators(trace, [{"name": "error_free"}, {"name": "tool_success"}])
        return {"results": [r.model_dump(mode="json") for r in res]}

    def _security_analyst(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        from forge_lens.security.mcp_inspect import inspect_trace

        return inspect_trace(trace)

    def _report_specialist(trace: Trace, ctx: dict[str, Any]) -> dict[str, Any]:
        return {"report": "see prior findings", "span_count": len(trace.spans)}

    return {
        "trace_investigator": _trace_investigator,
        "tool_execution_analyst": _tool_analyst,
        "context_efficiency_analyst": _context_analyst,
        "cost_analyst": _cost_analyst,
        "loop_detection_specialist": _loop_specialist,
        "failure_investigator": _failure_investigator,
        "evaluation_analyst": _eval_analyst,
        "security_observation_analyst": _security_analyst,
        "evidence_validator": _evidence_validator,
        "report_specialist": _report_specialist,
        "graph_analyst": _graph_analyst,
    }


SPECIALISTS = tuple(sorted(_registry()))


def run_specialist(
    specialist: str,
    trace: Trace,
    *,
    budget: AgentBudget | None = None,
    context: dict[str, Any] | None = None,
    clock: Callable[[], float] = time.monotonic,
    run_id: str = "run",
) -> AgentRun:
    """Run a registered specialist under budget/cancellation bounds.

    Budgets are enforced cooperatively — the tool boundary is checked
    before every iteration; a specialist returning early is `completed`,
    budget exhaustion is `budget_exceeded`, `run.cancel()` → `cancelled`.
    """
    budget = budget or AgentBudget()
    ctx = context or {}
    run = AgentRun(run_id=run_id, specialist=specialist)
    fn = _registry().get(specialist)
    if fn is None:
        run.stop_reason = "unknown_specialist"
        run.findings.append({"error": f"unknown specialist {specialist!r}",
                             "available": list(SPECIALISTS)})
        return run
    start = clock()
    for _ in range(budget.max_iterations):
        if run._cancel:
            run.cancelled = True
            run.stop_reason = "cancelled"
            break
        if clock() - start > budget.timeout_s:
            run.stop_reason = "timeout"
            break
        finding = fn(trace, ctx)
        run.iterations += 1
        run.findings.append(finding)
        run.tokens_observed += int(finding.get("tokens_observed") or 0)
        if run.tokens_observed >= budget.token_budget:
            run.stop_reason = "token_budget_exceeded"
            break
        break  # deterministic specialists do their work in one pass —
        # the loop exists so future multi-step specialists stay bounded
    else:
        # loop exhausted all iterations without break → budget hit
        run.stop_reason = "budget_exceeded"
    return run
