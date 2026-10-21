"""Trace exporter — stdlib-only HTTP client for the native ingest API.

Deliberately uses ``urllib`` (stdlib) so the SDK has zero hard
dependencies. Network access lives only in this module — the rest of the
SDK is pure construction of contract objects.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from forge_lens.contracts.v1.trace import Trace, TraceIngest, TraceSource


class ExportError(Exception):
    pass


class InMemoryExporter:
    """Collects flushed traces — for tests, offline runs, file export."""

    def __init__(self) -> None:
        self.traces: list[Trace] = []

    def export(
        self, traces: list[Trace], source: TraceSource | None = None
    ) -> dict[str, Any]:
        self.traces.extend(traces)
        return {
            "accepted_spans": sum(len(t.spans) for t in traces),
            "traces": [t.trace_id for t in traces],
        }


class HTTPExporter:
    """POSTs TraceIngest envelopes to ``<endpoint>/v1/traces``."""

    def __init__(
        self,
        endpoint: str = "http://127.0.0.1:7777",
        *,
        api_key: str | None = None,
        project_id: str = "default",
        timeout_s: float = 10.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.project_id = project_id
        self.timeout_s = timeout_s

    def export(self, traces: list[Trace], source: TraceSource | None = None) -> dict[str, Any]:
        envelope = TraceIngest(
            project_id=self.project_id, source=source, traces=traces
        )
        body = envelope.model_dump_json().encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["X-Forge-Lens-Key"] = self.api_key
        # S310: the endpoint is operator-configured (default http://127.0.0.1:7777);
        # restricting schemes here would break legitimate https:// deployments.
        req = urllib.request.Request(  # noqa: S310
            f"{self.endpoint}/v1/traces", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                result: dict[str, Any] = json.loads(resp.read().decode("utf-8"))
                return result
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ExportError(f"export failed HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ExportError(f"export failed: {exc.reason}") from exc
