"""Evaluation engine (Cycle 06 backend).

Deterministic evaluators over stored trace evidence, a runner that
produces ``EvaluationResult`` rows, and A/B comparison with an honest
statistical gate. No LLM-as-judge here — every score is computed from
observed spans and names its evidence.
"""

from forge_lens.eval.compare import compare_runs
from forge_lens.eval.evaluators import EVALUATORS, run_evaluators
from forge_lens.eval.runner import EvalReport, evaluate_traces

__all__ = [
    "EVALUATORS",
    "EvalReport",
    "compare_runs",
    "evaluate_traces",
    "run_evaluators",
]
