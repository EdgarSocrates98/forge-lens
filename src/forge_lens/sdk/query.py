"""Query client — stdlib-only HTTP reader for the Forge Lens API.

Mirror of :mod:`forge_lens.sdk.exporter` (the write side): ``urllib``
only, zero extra dependencies, and all network I/O kept inside this
module. Each method maps 1:1 onto a ``/v1`` read route and returns the
parsed JSON body verbatim — dicts/lists exactly as the API emits them,
nothing fabricated or recomputed locally.

The "read" surface includes the ``POST`` routes that take arguments as
a JSON body instead of query params (``eval/run``, ``eval/compare``,
``intel/efficiency``, ``intel/regress``, ``analyze``, ``alerts/evaluate``)
— they sit under the API's read auth dependency, so they belong here.

Auth mirrors the ingest path: when ``api_key`` is set it travels as the
``X-Forge-Lens-Key`` header (a ``reader`` role suffices). Reads are open
in the default local-first mode, so a key is optional unless the server
runs ``auth_reads`` or ``auth_mode="keys"``.

Deliberately not covered: ``GET /v1/traces/{id}/stream`` (SSE replay —
an event stream, not a JSON resource) and the ingest/admin routes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Literal

from forge_lens.core.config import LensConfig, load_config
from forge_lens.core.time import ensure_aware

__all__ = ["LensClient", "QueryError"]

_API_PREFIX = "/v1"

TraceStatus = Literal["error", "ok", "unset"]


class QueryError(Exception):
    """The one failure type for the read client: transport errors, HTTP
    error statuses and non-JSON responses all surface here.

    ``status_code`` holds the server's HTTP status when it answered
    (401/403/404/422…) and stays ``None`` for pure transport failures
    (connection refused, timeout).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _seg(value: str) -> str:
    """URL-escape one path segment — trace/session/dataset/version ids
    are caller data, never assumed URL-safe."""
    return urllib.parse.quote(value, safe="")


def _iso(value: str | datetime | None) -> str | None:
    """``datetime`` input → ISO-8601 (naive-as-UTC, same convention as
    the server's ``since``/``until`` parsing); strings pass through."""
    if isinstance(value, datetime):
        return ensure_aware(value).isoformat()
    return value


class LensClient:
    """Thin read client for the Forge Lens ``/v1`` query surface.

    ``project_id`` works like the CLI's ``--project`` and like
    ``HTTPExporter.project_id``: an explicit per-call value wins,
    otherwise the client's configured project is sent on every request
    (a scoped ``reader`` key must set the project it covers).
    """

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

    @classmethod
    def from_config(
        cls, config: LensConfig | None = None, *, timeout_s: float = 10.0
    ) -> LensClient:
        """Build from a :class:`LensConfig` — or resolve one via
        ``load_config()``, the same ``FORGE_LENS_*`` env +
        ``.forge-lens/config.toml`` chain the CLI and ``serve`` use
        (``endpoint``/``api_key``/``project_id``)."""
        cfg = config or load_config()
        return cls(
            endpoint=cfg.endpoint,
            api_key=cfg.api_key,
            project_id=cfg.project_id,
            timeout_s=timeout_s,
        )

    # -- transport ------------------------------------------------------

    def _pid(self, project_id: str | None) -> str:
        return project_id or self.project_id

    def _url(self, path: str, params: dict[str, Any] | None) -> str:
        url = f"{self.endpoint}{_API_PREFIX}{path}"
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
            if qs:
                url = f"{url}?{qs}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Forge-Lens-Key"] = self.api_key
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        # S310: the endpoint is operator-configured (default loopback);
        # restricting schemes would break legitimate https:// deployments
        # — same policy as sdk/exporter.py.
        req = urllib.request.Request(  # noqa: S310
            self._url(path, params), data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise QueryError(
                f"{method} {path} failed: HTTP {exc.code}: {detail}",
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise QueryError(f"{method} {path} failed: {exc.reason}") from exc
        except OSError as exc:
            # timeouts/resets can surface as bare OSError, not URLError
            raise QueryError(f"{method} {path} failed: {exc}") from exc
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise QueryError(
                f"{method} {path} failed: response is not JSON"
            ) from exc

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", path, params=params)
        return result

    def _get_list(
        self, path: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._request("GET", path, params=params)
        return result

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._request("POST", path, body=body)
        return result

    # -- meta -----------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /v1/health`` → ``{status, version, storage, stats?}``."""
        return self._get("/health")

    def metrics(self) -> dict[str, Any]:
        """``GET /v1/metrics`` → self-observability snapshot."""
        return self._get("/metrics")

    def capabilities(self) -> dict[str, Any]:
        """``GET /v1/capabilities`` → host adapter capability matrix."""
        return self._get("/capabilities")

    def pricing(self) -> dict[str, Any]:
        """``GET /v1/analytics/pricing`` → effective pricing table."""
        return self._get("/analytics/pricing")

    def ops_summary(self, *, project_id: str | None = None) -> dict[str, Any]:
        """``GET /v1/ops/summary`` → stats + recent-failure roll-up."""
        return self._get("/ops/summary", {"project_id": self._pid(project_id)})

    # -- traces ----------------------------------------------------------

    def list_traces(
        self,
        *,
        project_id: str | None = None,
        session_id: str | None = None,
        status: TraceStatus | None = None,
        since: str | datetime | None = None,
        until: str | datetime | None = None,
        min_duration_ms: float | None = None,
        max_duration_ms: float | None = None,
        q: str | None = None,
        tool: str | None = None,
        model: str | None = None,
        agent: str | None = None,
        host: str | None = None,
        min_total_tokens: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``GET /v1/traces`` → ``{items, limit, offset}``.

        Filters mirror the route exactly: AND'd, ``since``/``until`` are
        ISO-8601 (or ``datetime``, read naive-as-UTC), durations in ms.
        ``model`` matches request/response model attrs; ``agent`` matches
        ``gen_ai.agent.name``/producer identity; ``host`` matches
        ``source.host``/``host.name``; ``min_total_tokens`` bounds summed
        ``gen_ai.usage.total_tokens``. No ``cost`` filter — cost is
        derived from pricing at read time, never a stored predicate.
        """
        return self._get(
            "/traces",
            {
                "project_id": self._pid(project_id),
                "session_id": session_id,
                "status": status,
                "since": _iso(since),
                "until": _iso(until),
                "min_duration_ms": min_duration_ms,
                "max_duration_ms": max_duration_ms,
                "q": q,
                "tool": tool,
                "model": model,
                "agent": agent,
                "host": host,
                "min_total_tokens": min_total_tokens,
                "limit": limit,
                "offset": offset,
            },
        )

    def get_trace(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}`` → full ``Trace`` contract; a missing
        trace surfaces as ``QueryError`` with ``status_code == 404``."""
        return self._get(
            f"/traces/{_seg(trace_id)}", {"project_id": self._pid(project_id)}
        )

    def list_sessions(
        self,
        *,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``GET /v1/sessions`` → ``{items, limit, offset}`` roll-up."""
        return self._get(
            "/sessions",
            {
                "project_id": self._pid(project_id),
                "limit": limit,
                "offset": offset,
            },
        )

    def list_tools(
        self,
        *,
        project_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``GET /v1/tools`` → ``{items, limit, offset}`` fleet roll-up."""
        return self._get(
            "/tools",
            {
                "project_id": self._pid(project_id),
                "limit": limit,
                "offset": offset,
            },
        )

    # -- per-trace intelligence / investigation --------------------------

    def trace_graph(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/graph/traces/{id}`` → ``ExecutionGraph``."""
        return self._get(
            f"/graph/traces/{_seg(trace_id)}",
            {"project_id": self._pid(project_id)},
        )

    def trace_critical_path(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/graph/traces/{id}/critical-path`` →
        ``{path, total_ms, cycles}``."""
        return self._get(
            f"/graph/traces/{_seg(trace_id)}/critical-path",
            {"project_id": self._pid(project_id)},
        )

    def trace_bottlenecks(
        self, trace_id: str, *, project_id: str | None = None, top: int = 5
    ) -> dict[str, Any]:
        """``GET /v1/graph/traces/{id}/bottlenecks`` → ``{items}``
        self-time ranking."""
        return self._get(
            f"/graph/traces/{_seg(trace_id)}/bottlenecks",
            {"project_id": self._pid(project_id), "top": top},
        )

    def trace_graph_analysis(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/graph/traces/{id}/analysis`` → centrality/concentration
        analysis over the execution graph."""
        return self._get(
            f"/graph/traces/{_seg(trace_id)}/analysis",
            {"project_id": self._pid(project_id)},
        )

    def session_graph(
        self, session_id: str, *, project_id: str | None = None, limit: int = 200
    ) -> dict[str, Any]:
        """``GET /v1/graph/sessions/{id}`` → session execution graph
        (``stats.truncated`` marks windows over ``limit``)."""
        return self._get(
            f"/graph/sessions/{_seg(session_id)}",
            {"project_id": self._pid(project_id), "limit": limit},
        )

    def trace_cost(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/cost`` → derived cost report +
        ``pricing_source`` + ``waste[]``."""
        return self._get(
            f"/traces/{_seg(trace_id)}/cost",
            {"project_id": self._pid(project_id)},
        )

    def trace_failure(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/failure`` → deterministic failure
        classification with evidence."""
        return self._get(
            f"/traces/{_seg(trace_id)}/failure",
            {"project_id": self._pid(project_id)},
        )

    def trace_replay(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/replay`` → recorded timeline (never
        re-executes agent code)."""
        return self._get(
            f"/traces/{_seg(trace_id)}/replay",
            {"project_id": self._pid(project_id)},
        )

    def trace_incident(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/incident`` → versioned incident bundle
        (failure + graph + cost + waste + replay in one payload)."""
        return self._get(
            f"/traces/{_seg(trace_id)}/incident",
            {"project_id": self._pid(project_id)},
        )

    def trace_security(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/security`` → MCP/tool-call observations
        (observer mode, evidence only)."""
        return self._get(
            f"/traces/{_seg(trace_id)}/security",
            {"project_id": self._pid(project_id)},
        )

    def trace_eval(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/eval`` → default evaluator battery →
        ``{trace_id, results}``."""
        return self._get(
            f"/traces/{_seg(trace_id)}/eval",
            {"project_id": self._pid(project_id)},
        )

    def trace_efficiency(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/efficiency`` → tokens/tools/retries
        roll-up for one trace."""
        return self._get(
            f"/traces/{_seg(trace_id)}/efficiency",
            {"project_id": self._pid(project_id)},
        )

    def trace_recommendations(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/recommendations`` → candidate
        recommendations with evidence."""
        return self._get(
            f"/traces/{_seg(trace_id)}/recommendations",
            {"project_id": self._pid(project_id)},
        )

    def trace_correlate(
        self, trace_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/correlate`` → temporal co-occurrence
        signals (non-causal; see ``classification``/``caveat`` fields)."""
        return self._get(
            f"/traces/{_seg(trace_id)}/correlate",
            {"project_id": self._pid(project_id)},
        )

    def trace_loops(
        self,
        trace_id: str,
        *,
        project_id: str | None = None,
        repeat_threshold: int = 3,
    ) -> dict[str, Any]:
        """``GET /v1/traces/{id}/loops`` → heuristic loop/no-progress
        findings with span evidence."""
        return self._get(
            f"/traces/{_seg(trace_id)}/loops",
            {
                "project_id": self._pid(project_id),
                "repeat_threshold": repeat_threshold,
            },
        )

    # -- datasets ---------------------------------------------------------

    def list_datasets(
        self, *, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        """``GET /v1/datasets`` → bare array (this route has no
        ``items`` envelope, unlike the paginated roll-ups)."""
        return self._get_list(
            "/datasets", {"project_id": self._pid(project_id)}
        )

    def get_dataset(
        self, dataset_id: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/datasets/{id}`` → dataset metadata; 404 → QueryError."""
        return self._get(
            f"/datasets/{_seg(dataset_id)}",
            {"project_id": self._pid(project_id)},
        )

    def list_dataset_versions(
        self, dataset_id: str, *, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        """``GET /v1/datasets/{id}/versions`` → bare array; 404 if the
        dataset does not exist."""
        return self._get_list(
            f"/datasets/{_seg(dataset_id)}/versions",
            {"project_id": self._pid(project_id)},
        )

    def get_dataset_version(
        self, dataset_id: str, version: str, *, project_id: str | None = None
    ) -> dict[str, Any]:
        """``GET /v1/datasets/{id}/versions/{version}`` → one immutable
        version; 404 → QueryError."""
        return self._get(
            f"/datasets/{_seg(dataset_id)}/versions/{_seg(version)}",
            {"project_id": self._pid(project_id)},
        )

    # -- read-side POSTs (bodies, not query params) -----------------------

    def eval_run(
        self,
        trace_ids: list[str],
        evaluators: list[dict[str, Any]],
        *,
        dataset: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/eval/run`` → ``EvalReport`` (``results`` +
        per-evaluator ``summary``)."""
        body: dict[str, Any] = {
            "trace_ids": list(trace_ids),
            "evaluators": evaluators,
            "project_id": self._pid(project_id),
        }
        if dataset is not None:
            body["dataset"] = dataset
        return self._post("/eval/run", body)

    def eval_compare(
        self,
        a: dict[str, Any],
        b: dict[str, Any],
        evaluators: list[dict[str, Any]],
        *,
        gate: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/eval/compare`` — ``a``/``b`` are side objects
        (``{trace_ids, dataset?, project_id?}``); a side's own
        ``project_id`` overrides the top-level one for that side only."""
        body: dict[str, Any] = {
            "a": a,
            "b": b,
            "evaluators": evaluators,
            "project_id": self._pid(project_id),
        }
        if gate is not None:
            body["gate"] = gate
        return self._post("/eval/compare", body)

    def fleet_efficiency(
        self,
        trace_ids: list[str] | None = None,
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/intel/efficiency`` — fleet roll-up over explicit
        ``trace_ids`` or, when omitted, the bounded recent window."""
        body: dict[str, Any] = {"project_id": self._pid(project_id)}
        if trace_ids is not None:
            body["trace_ids"] = list(trace_ids)
        return self._post("/intel/efficiency", body)

    def fleet_correlate(
        self,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/intel/correlate`` — fleet co-occurrence tables
        over the bounded recent window (descriptive, non-causal)."""
        body: dict[str, Any] = {"project_id": self._pid(project_id)}
        if limit is not None:
            body["limit"] = limit
        return self._post("/intel/correlate", body)

    def compare_cohorts(
        self,
        a: dict[str, Any],
        b: dict[str, Any],
        *,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/intel/regress`` — cohort deltas over two
        ``{trace_ids, project_id?}`` sides; observed differences, not
        significance."""
        return self._post(
            "/intel/regress",
            {"a": a, "b": b, "project_id": self._pid(project_id)},
        )

    def list_specialists(self) -> dict[str, Any]:
        """``GET /v1/analyze/specialists`` → registered specialist ids."""
        return self._get("/analyze/specialists")

    def run_analysis(
        self,
        specialist: str,
        trace_id: str,
        *,
        budget: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        run_id: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/analyze/{specialist}/{trace_id}`` — run a registered
        specialist under ``budget`` (``max_iterations``/``token_budget``/
        ``timeout_s``); ``context`` is merged server-side with the live
        pricing registry."""
        body: dict[str, Any] = {"project_id": self._pid(project_id)}
        if budget is not None:
            body["budget"] = budget
        if context is not None:
            body["context"] = context
        if run_id is not None:
            body["run_id"] = run_id
        return self._post(f"/analyze/{_seg(specialist)}/{_seg(trace_id)}", body)

    def alerts_evaluate(
        self,
        rules: list[dict[str, Any]] | None = None,
        *,
        use_saved: bool = False,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """``POST /v1/alerts/evaluate`` — threshold rules evaluated
        against store metrics at request time."""
        return self._post(
            "/alerts/evaluate",
            {
                "rules": list(rules or []),
                "use_saved": use_saved,
                "project_id": self._pid(project_id),
            },
        )
