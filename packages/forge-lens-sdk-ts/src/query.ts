/** LensClient — read-side query client for the Forge Lens `/v1` API.
 *
 *  Mirror of the Python `LensClient` (forge_lens.sdk.query) and the
 *  read-side counterpart of `HTTPExporter` (the write side): fetch
 *  only, zero extra dependencies, all network I/O kept inside this
 *  module. Each method maps 1:1 onto a `/v1` read route and returns
 *  the parsed JSON body verbatim — objects/arrays exactly as the API
 *  emits them, nothing fabricated or recomputed locally.
 *
 *  The "read" surface includes the POST routes that take arguments as
 *  a JSON body instead of query params (`eval/run`, `eval/compare`,
 *  `intel/efficiency`, `intel/regress`, `analyze`, `alerts/evaluate`)
 *  — they sit under the API's read auth dependency, so they belong here.
 *
 *  Auth mirrors the ingest path: when `apiKey` is set it travels as the
 *  `X-Forge-Lens-Key` header (a `reader` role suffices). Reads are open
 *  in the default local-first mode, so a key is optional unless the
 *  server runs `auth_reads` or `auth_mode="keys"`.
 *
 *  Deliberately not covered: `GET /v1/traces/{id}/stream` (SSE replay —
 *  an event stream, not a JSON resource) and the ingest/admin routes.
 */

import type { StatusCode, Trace } from "./contracts.js";

/** Loose JSON object — the wire shape of most read responses. */
export type JsonObject = Record<string, unknown>;

/** Paginated roll-up envelope used by /traces, /sessions, /tools. */
export interface ListEnvelope<T = JsonObject> {
  items: T[];
  limit: number;
  offset: number;
}

/** `GET /v1/health` — `stats` is absent for multi-project keys. */
export interface HealthResponse {
  status: string;
  version: string;
  storage: string;
  stats?: JsonObject;
}

/** `GET /v1/traces/{id}/eval` → `{trace_id, results}`. */
export interface TraceEvalResponse {
  trace_id: string;
  results: JsonObject[];
}

/** `GET /v1/analyze/specialists` → `{specialists}`. */
export interface SpecialistsResponse {
  specialists: string[];
}

/** One side of `evalCompare` — `{trace_ids, dataset?, project_id?}`;
 *  a side's own `project_id` overrides the top-level one for that
 *  side only (cross-project A/B). */
export interface EvalSide {
  trace_ids: string[];
  dataset?: string;
  project_id?: string;
}

/** One side of `compareCohorts` — `{trace_ids, project_id?}`. */
export interface CohortSide {
  trace_ids: string[];
  project_id?: string;
}

/** Per-call options — `projectId` overrides the client's configured
 *  project for that call only (same rule as the CLI's `--project` and
 *  `HTTPExporter.projectId`). */
export interface ProjectScoped {
  projectId?: string;
}

export interface ListTracesFilters extends ProjectScoped {
  sessionId?: string;
  status?: StatusCode;
  /** ISO-8601 string or `Date` (serialized via `toISOString()`). */
  since?: string | Date;
  until?: string | Date;
  minDurationMs?: number;
  maxDurationMs?: number;
  q?: string;
  tool?: string;
  limit?: number; // default 50
  offset?: number; // default 0
}

export interface ListPageOptions extends ProjectScoped {
  limit?: number;
  offset?: number;
}

export interface LensClientOptions {
  /** Base URL of the Forge Lens API (default `http://127.0.0.1:7777`). */
  endpoint?: string;
  /** Sent as `X-Forge-Lens-Key` when set — a `reader` role suffices. */
  apiKey?: string;
  /** Project sent on every request unless overridden per call. */
  projectId?: string;
  /** Abort a request after this many ms (default 10_000). */
  timeoutMs?: number;
  /** Injectable for tests/proxies (default: global `fetch`). */
  fetchImpl?: typeof fetch;
}

/** The one failure type for the read client: transport errors, HTTP
 *  error statuses, timeouts and non-JSON responses all surface here.
 *
 *  `statusCode` holds the server's HTTP status when it answered
 *  (401/403/404/422…) and stays `undefined` for pure transport
 *  failures (connection refused, timeout). */
export class LensQueryError extends Error {
  readonly statusCode?: number;

  constructor(
    message: string,
    opts: { statusCode?: number; cause?: unknown } = {},
  ) {
    super(message);
    this.name = "LensQueryError";
    this.statusCode = opts.statusCode;
    if (opts.cause !== undefined) this.cause = opts.cause;
  }
}

/** URL-escape one path segment — trace/session/dataset/version ids
 *  are caller data, never assumed URL-safe. */
function seg(value: string): string {
  return encodeURIComponent(value);
}

/** `Date` → ISO-8601; strings pass through (same convention as the
 *  server's `since`/`until` parsing). */
function iso(value: string | Date | undefined): string | undefined {
  return value instanceof Date ? value.toISOString() : value;
}

export class LensClient {
  /** Base URL with trailing slashes stripped. */
  readonly endpoint: string;
  readonly apiKey?: string;
  readonly projectId: string;
  readonly timeoutMs: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: LensClientOptions = {}) {
    this.endpoint = (opts.endpoint ?? "http://127.0.0.1:7777").replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.projectId = opts.projectId ?? "default";
    this.timeoutMs = opts.timeoutMs ?? 10_000;
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  // -- transport ------------------------------------------------------

  private pid(projectId: string | undefined): string {
    // `||` mirrors Python's `project_id or self.project_id` — an empty
    // per-call value falls back to the configured project
    return projectId || this.projectId;
  }

  private url(path: string, params?: Record<string, unknown>): string {
    let url = `${this.endpoint}/v1${path}`;
    if (params) {
      const qs = new URLSearchParams();
      for (const [key, value] of Object.entries(params)) {
        if (value === undefined || value === null) continue;
        qs.set(key, String(value));
      }
      const s = qs.toString();
      if (s) url += `?${s}`;
    }
    return url;
  }

  private async request(
    method: "GET" | "POST",
    path: string,
    opts: { params?: Record<string, unknown>; body?: JsonObject } = {},
  ): Promise<unknown> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (this.apiKey) headers["X-Forge-Lens-Key"] = this.apiKey;
    let data: string | undefined;
    if (opts.body !== undefined) {
      data = JSON.stringify(opts.body);
      headers["Content-Type"] = "application/json";
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), this.timeoutMs);
    let resp: Response;
    try {
      resp = await Promise.race([
        this.fetchImpl(this.url(path, opts.params), {
          method,
          headers,
          body: data,
          signal: controller.signal,
        }),
        // race even when fetchImpl ignores the signal — the deadline
        // is the client's contract, not the transport's courtesy;
        // race() keeps a rejection handler on the fetch promise, so a
        // late rejection after the timeout wins is never unhandled
        new Promise<never>((_resolve, reject) => {
          controller.signal.addEventListener("abort", () => {
            reject(
              new LensQueryError(
                `${method} ${path} failed: timed out after ${this.timeoutMs}ms`,
              ),
            );
          });
        }),
      ]);
    } catch (err) {
      if (err instanceof LensQueryError) throw err;
      if (controller.signal.aborted) {
        throw new LensQueryError(
          `${method} ${path} failed: timed out after ${this.timeoutMs}ms`,
          { cause: err },
        );
      }
      const detail = err instanceof Error ? err.message : String(err);
      throw new LensQueryError(`${method} ${path} failed: ${detail}`, {
        cause: err,
      });
    } finally {
      clearTimeout(timer);
    }
    if (!resp.ok) {
      const detail = (await resp.text()).slice(0, 500);
      throw new LensQueryError(
        `${method} ${path} failed: HTTP ${resp.status}: ${detail}`,
        { statusCode: resp.status },
      );
    }
    try {
      return await resp.json();
    } catch (err) {
      throw new LensQueryError(`${method} ${path} failed: response is not JSON`, {
        cause: err,
      });
    }
  }

  private async get<T>(path: string, params?: Record<string, unknown>): Promise<T> {
    return (await this.request("GET", path, { params })) as T;
  }

  private async post<T>(path: string, body: JsonObject): Promise<T> {
    return (await this.request("POST", path, { body })) as T;
  }

  // -- meta -----------------------------------------------------------

  /** `GET /v1/health` → `{status, version, storage, stats?}`. */
  health(): Promise<HealthResponse> {
    return this.get("/health");
  }

  /** `GET /v1/metrics` → self-observability snapshot. */
  metrics(): Promise<JsonObject> {
    return this.get("/metrics");
  }

  /** `GET /v1/capabilities` → host adapter capability matrix. */
  capabilities(): Promise<JsonObject> {
    return this.get("/capabilities");
  }

  /** `GET /v1/analytics/pricing` → effective pricing table. */
  pricing(): Promise<JsonObject> {
    return this.get("/analytics/pricing");
  }

  /** `GET /v1/ops/summary` → stats + recent-failure roll-up. */
  opsSummary(opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get("/ops/summary", { project_id: this.pid(opts.projectId) });
  }

  // -- traces ---------------------------------------------------------

  /** `GET /v1/traces` → `{items, limit, offset}`.
   *
   *  Filters mirror the route exactly: AND'd, `since`/`until` are
   *  ISO-8601 (or `Date`), durations in ms. */
  listTraces(filters: ListTracesFilters = {}): Promise<ListEnvelope> {
    return this.get("/traces", {
      project_id: this.pid(filters.projectId),
      session_id: filters.sessionId,
      status: filters.status,
      since: iso(filters.since),
      until: iso(filters.until),
      min_duration_ms: filters.minDurationMs,
      max_duration_ms: filters.maxDurationMs,
      q: filters.q,
      tool: filters.tool,
      limit: filters.limit ?? 50,
      offset: filters.offset ?? 0,
    });
  }

  /** `GET /v1/traces/{id}` → full `Trace` contract; a missing trace
   *  surfaces as `LensQueryError` with `statusCode === 404`. */
  getTrace(traceId: string, opts: ProjectScoped = {}): Promise<Trace> {
    return this.get(`/traces/${seg(traceId)}`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/sessions` → `{items, limit, offset}` roll-up. */
  listSessions(opts: ListPageOptions = {}): Promise<ListEnvelope> {
    return this.get("/sessions", {
      project_id: this.pid(opts.projectId),
      limit: opts.limit ?? 100,
      offset: opts.offset ?? 0,
    });
  }

  /** `GET /v1/tools` → `{items, limit, offset}` fleet roll-up. */
  listTools(opts: ListPageOptions = {}): Promise<ListEnvelope> {
    return this.get("/tools", {
      project_id: this.pid(opts.projectId),
      limit: opts.limit ?? 100,
      offset: opts.offset ?? 0,
    });
  }

  // -- per-trace intelligence / investigation -------------------------

  /** `GET /v1/graph/traces/{id}` → `ExecutionGraph`. */
  traceGraph(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/graph/traces/${seg(traceId)}`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/graph/traces/{id}/critical-path` →
   *  `{path, total_ms, cycles}`. */
  traceCriticalPath(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/graph/traces/${seg(traceId)}/critical-path`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/graph/traces/{id}/bottlenecks` → `{items}` self-time
   *  ranking. */
  traceBottlenecks(
    traceId: string,
    opts: ProjectScoped & { top?: number } = {},
  ): Promise<JsonObject> {
    return this.get(`/graph/traces/${seg(traceId)}/bottlenecks`, {
      project_id: this.pid(opts.projectId),
      top: opts.top ?? 5,
    });
  }

  /** `GET /v1/graph/traces/{id}/analysis` → centrality/concentration
   *  analysis over the execution graph. */
  traceGraphAnalysis(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/graph/traces/${seg(traceId)}/analysis`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/graph/sessions/{id}` → session execution graph
   *  (`stats.truncated` marks windows over `limit`). */
  sessionGraph(
    sessionId: string,
    opts: ListPageOptions = {},
  ): Promise<JsonObject> {
    return this.get(`/graph/sessions/${seg(sessionId)}`, {
      project_id: this.pid(opts.projectId),
      limit: opts.limit ?? 200,
    });
  }

  /** `GET /v1/traces/{id}/cost` → derived cost report +
   *  `pricing_source` + `waste[]`. */
  traceCost(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/cost`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/failure` → deterministic failure
   *  classification with evidence. */
  traceFailure(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/failure`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/replay` → recorded timeline (never
   *  re-executes agent code). */
  traceReplay(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/replay`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/incident` → versioned incident bundle
   *  (failure + graph + cost + waste + replay in one payload). */
  traceIncident(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/incident`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/security` → MCP/tool-call observations
   *  (observer mode, evidence only). */
  traceSecurity(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/security`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/eval` → default evaluator battery →
   *  `{trace_id, results}`. */
  traceEval(traceId: string, opts: ProjectScoped = {}): Promise<TraceEvalResponse> {
    return this.get(`/traces/${seg(traceId)}/eval`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/efficiency` → tokens/tools/retries roll-up
   *  for one trace. */
  traceEfficiency(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/efficiency`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/recommendations` → candidate recommendations
   *  with evidence. */
  traceRecommendations(traceId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/recommendations`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/traces/{id}/loops` → heuristic loop/no-progress findings
   *  with span evidence. */
  traceLoops(
    traceId: string,
    opts: ProjectScoped & { repeatThreshold?: number } = {},
  ): Promise<JsonObject> {
    return this.get(`/traces/${seg(traceId)}/loops`, {
      project_id: this.pid(opts.projectId),
      repeat_threshold: opts.repeatThreshold ?? 3,
    });
  }

  // -- datasets --------------------------------------------------------

  /** `GET /v1/datasets` → bare array (this route has no `items`
   *  envelope, unlike the paginated roll-ups). */
  listDatasets(opts: ProjectScoped = {}): Promise<JsonObject[]> {
    return this.get("/datasets", { project_id: this.pid(opts.projectId) });
  }

  /** `GET /v1/datasets/{id}` → dataset metadata; 404 → LensQueryError. */
  getDataset(datasetId: string, opts: ProjectScoped = {}): Promise<JsonObject> {
    return this.get(`/datasets/${seg(datasetId)}`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/datasets/{id}/versions` → bare array; 404 if the dataset
   *  does not exist. */
  listDatasetVersions(
    datasetId: string,
    opts: ProjectScoped = {},
  ): Promise<JsonObject[]> {
    return this.get(`/datasets/${seg(datasetId)}/versions`, {
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/datasets/{id}/versions/{version}` → one immutable version;
   *  404 → LensQueryError. */
  getDatasetVersion(
    datasetId: string,
    version: string,
    opts: ProjectScoped = {},
  ): Promise<JsonObject> {
    return this.get(
      `/datasets/${seg(datasetId)}/versions/${seg(version)}`,
      { project_id: this.pid(opts.projectId) },
    );
  }

  // -- read-side POSTs (bodies, not query params) ----------------------

  /** `POST /v1/eval/run` → `EvalReport` (`results` + per-evaluator
   *  `summary`). */
  evalRun(
    traceIds: string[],
    evaluators: JsonObject[],
    opts: ProjectScoped & { dataset?: string } = {},
  ): Promise<JsonObject> {
    const body: JsonObject = {
      trace_ids: [...traceIds],
      evaluators,
      project_id: this.pid(opts.projectId),
    };
    if (opts.dataset !== undefined) body.dataset = opts.dataset;
    return this.post("/eval/run", body);
  }

  /** `POST /v1/eval/compare` — `a`/`b` are side objects
   *  (`{trace_ids, dataset?, project_id?}`); a side's own `project_id`
   *  overrides the top-level one for that side only. */
  evalCompare(
    a: EvalSide,
    b: EvalSide,
    evaluators: JsonObject[],
    opts: ProjectScoped & { gate?: JsonObject } = {},
  ): Promise<JsonObject> {
    const body: JsonObject = {
      a,
      b,
      evaluators,
      project_id: this.pid(opts.projectId),
    };
    if (opts.gate !== undefined) body.gate = opts.gate;
    return this.post("/eval/compare", body);
  }

  /** `POST /v1/intel/efficiency` — fleet roll-up over explicit
   *  `traceIds` or, when omitted, the bounded recent window. */
  fleetEfficiency(
    traceIds?: string[],
    opts: ProjectScoped = {},
  ): Promise<JsonObject> {
    const body: JsonObject = { project_id: this.pid(opts.projectId) };
    if (traceIds !== undefined) body.trace_ids = [...traceIds];
    return this.post("/intel/efficiency", body);
  }

  /** `POST /v1/intel/regress` — cohort deltas over two
   *  `{trace_ids, project_id?}` sides; observed differences, not
   *  significance. */
  compareCohorts(
    a: CohortSide,
    b: CohortSide,
    opts: ProjectScoped = {},
  ): Promise<JsonObject> {
    return this.post("/intel/regress", {
      a,
      b,
      project_id: this.pid(opts.projectId),
    });
  }

  /** `GET /v1/analyze/specialists` → registered specialist ids. */
  listSpecialists(): Promise<SpecialistsResponse> {
    return this.get("/analyze/specialists");
  }

  /** `POST /v1/analyze/{specialist}/{traceId}` — run a registered
   *  specialist under `budget` (`max_iterations`/`token_budget`/
   *  `timeout_s`); `context` is merged server-side with the live
   *  pricing registry. */
  runAnalysis(
    specialist: string,
    traceId: string,
    opts: ProjectScoped & {
      budget?: JsonObject;
      context?: JsonObject;
      runId?: string;
    } = {},
  ): Promise<JsonObject> {
    const body: JsonObject = { project_id: this.pid(opts.projectId) };
    if (opts.budget !== undefined) body.budget = opts.budget;
    if (opts.context !== undefined) body.context = opts.context;
    if (opts.runId !== undefined) body.run_id = opts.runId;
    return this.post(`/analyze/${seg(specialist)}/${seg(traceId)}`, body);
  }

  /** `POST /v1/alerts/evaluate` — threshold rules evaluated against
   *  store metrics at request time. */
  alertsEvaluate(
    rules?: JsonObject[],
    opts: ProjectScoped & { useSaved?: boolean } = {},
  ): Promise<JsonObject> {
    return this.post("/alerts/evaluate", {
      rules: rules ? [...rules] : [],
      use_saved: opts.useSaved ?? false,
      project_id: this.pid(opts.projectId),
    });
  }
}
