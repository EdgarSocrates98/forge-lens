"""MCP / tool-call security observer — Cycle 08 §08.10.

The Lens *observes* potentially dangerous calls; it never blocks at
instrumentation time (a future Agent-Shield integration may add
preventive policy). Every observation carries its evidence — the
span id and the attribute that triggered it — and a ``kind`` a
dashboard can group by.
"""

from __future__ import annotations

import re
from typing import Any

from forge_lens.contracts.v1.trace import Span, Trace

# tool names whose invocation deserves attention — not a denylist,
# an *observation* list: power tools are legitimate when intended
_RISKY_TOOL_RE = re.compile(
    r"(^|[^a-z])(exec|eval|shell|bash|subprocess|os\.system|"
    r"rm|delete|drop|destroy|write_file|apply_patch|sudo|"
    r"send_email|transfer|payment|refund)([^a-z]|$)",
    re.IGNORECASE,
)

# attribute *values* that look like embedded secrets — the key-level
# denylist redacts named fields; this catches secret-shaped values
# under innocuous names (same shapes as sanitize.VALUE_PATTERNS)
_SECRET_VALUE_RES = (
    re.compile(r"sk-[A-Za-z0-9\-_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

# path-traversal / sensitive-path hints in tool arguments
_PATH_RE = re.compile(r"(\.\./)|(^|[^a-z])(/etc/|/root/|~/\.ssh|\.env)([^a-z]|$)")


def _walk_strings(value: Any, depth: int = 0) -> list[str]:
    """Yield string leaves up to a bound — inspection never walks an
    unbounded payload."""
    if depth > 8:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in list(value.values())[:256]:
            out.extend(_walk_strings(v, depth + 1))
        return out
    if isinstance(value, list):
        out = []
        for v in value[:256]:
            out.extend(_walk_strings(v, depth + 1))
        return out
    return []


def inspect_span(span: Span) -> list[dict[str, Any]]:
    """Flag potentially dangerous calls on one span → observations."""
    out: list[dict[str, Any]] = []
    label = str(span.attributes.get("tool.name") or span.name)
    if _RISKY_TOOL_RE.search(label):
        out.append(
            {
                "kind": "risky_tool",
                "severity": "review",
                "span_id": span.span_id,
                "detail": f"tool name {label!r} matches the risky-verb watchlist",
            }
        )
    for key, value in span.attributes.items():
        if not isinstance(value, (str, dict, list)):
            continue
        for leaf in _walk_strings(value):
            if any(p.search(leaf) for p in _SECRET_VALUE_RES):
                out.append(
                    {
                        "kind": "secret_shaped_value",
                        "severity": "high",
                        "span_id": span.span_id,
                        "detail": f"attribute {key!r} holds a secret-shaped value"
                        " (survived key-level redaction — review the denylist)",
                    }
                )
                break  # one observation per attribute is enough
            if _PATH_RE.search(leaf):
                out.append(
                    {
                        "kind": "sensitive_path",
                        "severity": "review",
                        "span_id": span.span_id,
                        "detail": f"attribute {key!r} references a sensitive"
                        " path (../, /etc/, ~/.ssh, .env)",
                    }
                )
                break
    return out


def inspect_trace(trace: Trace) -> dict[str, Any]:
    """Trace-level MCP security report — observer findings, sorted."""
    obs: list[dict[str, Any]] = []
    for span in trace.spans:
        obs.extend(inspect_span(span))
    obs.sort(
        key=lambda o: (
            0 if o["severity"] == "high" else 1,
            o["kind"],
            o["span_id"],
        )
    )
    counts: dict[str, int] = {}
    for o in obs:
        counts[o["kind"]] = counts.get(o["kind"], 0) + 1
    return {
        "trace_id": trace.trace_id,
        "observations": obs,
        "counts": dict(sorted(counts.items())),
        "mode": "observer",
    }
