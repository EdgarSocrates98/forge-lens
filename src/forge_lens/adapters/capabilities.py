"""Honest host capability matrix — Cycle 10.

Every host entry states WHAT is actually instrumentable, through which
mechanism. 'unsupported' is a valid answer — the matrix exists so
nobody claims coverage that doesn't exist.
"""

from __future__ import annotations

from typing import Any

HOST_CAPABILITIES: dict[str, dict[str, Any]] = {
    "claude_code": {
        "native_tracing": False,
        "mechanism": "hooks + session JSONL logs",
        "adapter": "adapters.claude_code (JSONL import → Trace)",
        "coverage": "tool calls, model usage, session boundaries",
        "limits": "no in-process span capture; log-derived only",
    },
    "codex": {
        "native_tracing": False,
        "mechanism": "session logs / rollout files",
        "adapter": "not implemented — log format unstable",
        "coverage": "none claimed",
        "limits": "unsupported today",
    },
    "devin": {
        "native_tracing": False,
        "mechanism": "unsupported — no public instrumentation surface",
        "adapter": None,
        "coverage": "none",
        "limits": "unsupported",
    },
    "copilot": {
        "native_tracing": False,
        "mechanism": "unsupported — closed surface",
        "adapter": None,
        "coverage": "none",
        "limits": "unsupported",
    },
    "cursor": {
        "native_tracing": False,
        "mechanism": "MCP instrumentation point exists (client-side)",
        "adapter": "forge_lens.mcp_server used as gateway observer",
        "coverage": "MCP tool list/call/result only — NOT agent internals",
        "limits": "MCP traffic only; no claim of full tracing",
    },
    "forge": {
        "native_tracing": True,
        "mechanism": "in-process SDK instrumentation",
        "adapter": "forge_lens.sdk (native)",
        "coverage": "full spans, tools, tokens",
        "limits": "requires opt-in tracing init",
    },
    "generic_otlp": {
        "native_tracing": True,
        "mechanism": "OTLP/HTTP JSON endpoint",
        "adapter": "built-in",
        "coverage": "whatever the runtime emits",
        "limits": "span quality depends on emitter",
    },
}


def capability_matrix() -> dict[str, Any]:
    return {
        "hosts": {k: dict(v) for k, v in sorted(HOST_CAPABILITIES.items())},
        "note": "coverage claims are per-mechanism; 'unsupported' means"
                " no verified path exists today",
    }
