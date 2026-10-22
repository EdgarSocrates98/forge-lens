import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpParams, HttpHeaders } from '@angular/common/http';
import { Observable, map } from 'rxjs';

export interface TraceRow {
  trace_id: string; name?: string; status?: string;
  started_at?: string; ended_at?: string; span_count?: number;
}
export interface Span {
  span_id: string; parent_span_id?: string | null; name: string;
  kind: string; start_time: string; end_time?: string | null;
  status: { code: string; message?: string };
  attributes?: Record<string, unknown>;
}
export interface Trace {
  trace_id: string; spans: Span[]; status: { code: string };
}
export interface Capability {
  mechanism: string; coverage: string; limits: string;
}
export interface Capabilities {
  hosts: Record<string, Capability>; note: string;
}
export interface Finding { kind: string; classification: string; span_ids?: string[]; detail?: string }
export interface Loops { findings: Finding[]; classification: string; note?: string }
export interface OpsSummary {
  project_id: string | null; stats: Record<string, unknown>;
  recent_traces: number; recent_failures: string[]; note: string;
}
/** One fired rule in a POST /v1/alerts/evaluate response — the server
 *  only reports rules that fired; `evaluated` on the response counts
 *  every rule considered, so passing rules are summarized, not rowed. */
export interface AlertEvent {
  rule_id: string; metric: string; op: string;
  threshold: number; observed: number;
  severity: string; message: string;
}
/** POST /v1/alerts/evaluate response — `fired` events plus how many
 *  rules were evaluated. There is no rules-listing GET; rules persist
 *  via POST/DELETE /v1/alerts/rules and merge in via `use_saved`. */
export interface AlertEval {
  fired: AlertEvent[]; evaluated: number; project_id: string | null;
}
/** Server-side filters accepted by GET /v1/traces — empty/undefined omitted. */
export interface TraceFilter {
  status?: string; session_id?: string; tool?: string; q?: string;
  since?: string; until?: string;
  min_duration_ms?: number; max_duration_ms?: number;
  model?: string; agent?: string; host?: string;
  min_total_tokens?: number; project_id?: string;
  limit?: number; offset?: number;
}
/** Row shape returned by GET /v1/sessions (ordered last_activity_at DESC). */
export interface SessionRow {
  session_id: string; traces: number; spans: number;
  first_started_at: string; last_activity_at: string | null;
}
/** One EvaluationResult — `score: null` means no verdict; `details.reason`
 *  carries the evidence for why (unobserved field, missing params, …). */
export interface EvalResult {
  evaluator: string;
  evaluator_version?: string | null;
  metric: string;
  score: number | null;
  trace_reference?: string | null;
  evaluated_at?: string | null;
  details?: Record<string, string>;
}
/** GET /v1/traces/{id}/eval — the server's default no-param battery. */
export interface TraceEval {
  trace_id: string; results: EvalResult[];
}
/** One evaluator spec in a POST /v1/eval/run body — `params` is
 *  evaluator-specific (see forge_lens.eval.evaluators); omit it when a
 *  run carries none rather than posting `params: {}`. */
export interface EvalSpec {
  name: string;
  params?: Record<string, unknown>;
}
/** POST /v1/eval/run response — results keyed by trace_id. */
export interface EvalReport {
  dataset: string | null; trace_ids: string[];
  results: Record<string, EvalResult[]>;
  summary: Record<string, {
    n: number; nulls: number; sum: number;
    mean_score: number | null; pass_rate: number | null;
  }>;
}
/** Row from GET /v1/datasets — a bare JSON array (no items wrapper). */
export interface DatasetRow {
  dataset_id: string; project_id: string;
  name: string; task: string | null; created_at: string | null;
}
/** GET /v1/datasets/{id} — the list fields plus metadata. */
export interface DatasetDetail extends DatasetRow {
  metadata: Record<string, unknown>;
}
/** Row from GET /v1/datasets/{id}/versions — bare array, case_count
 *  joined server-side. */
export interface DatasetVersionRow {
  dataset_id: string; project_id: string; version: string;
  description: string | null; case_count: number;
  created_at: string | null;
}
/** GET /v1/datasets/{id}/versions/{v} — the version row plus its full
 *  ordered `cases` list (same field contract as DatasetCaseInput). */
export interface DatasetVersionDetail {
  dataset_id: string; project_id: string; version: string;
  description: string | null; created_at: string | null;
  metadata: Record<string, unknown>;
  cases: DatasetCaseInput[];
}
/** PUT /v1/datasets/{id} response. */
export interface DatasetStored {
  dataset_id: string; project_id: string; stored: boolean;
}
/** PUT /v1/datasets/{id}/versions/{v} response. */
export interface DatasetVersionStored {
  dataset_id: string; version: string; cases: number;
}
/** One case in a PUT version body — input/expected stay JSON objects
 *  (arbitrary shape) per the DatasetCase contract. */
export interface DatasetCaseInput {
  case_id: string;
  input?: Record<string, unknown>;
  expected?: Record<string, unknown>;
  allowed_tools?: string[];
  constraints?: Record<string, unknown>;
  rubric?: Record<string, unknown>;
}
/** One side of POST /v1/eval/compare. `trace_ids` is required (1..500
 *  stored ids — a missing id is a 404, never a silent skip); `dataset`
 *  is a label recorded on that side's report, NOT resolved to cases. */
export interface CompareSideSpec {
  trace_ids: string[];
  dataset?: string;
  project_id?: string;
}
/** POST /v1/eval/compare request body. */
export interface CompareRequest {
  a: CompareSideSpec; b: CompareSideSpec;
  evaluators: EvalSpec[];
  gate?: Record<string, unknown>;
  project_id?: string;
}
export interface CompareSideResult {
  dataset: string | null; trace_ids: string[]; n: number;
}
/** One compare_runs delta row — every nullable field means unmeasured
 *  (`null` is honest absence, never a zero to display). */
export interface MetricDelta {
  evaluator: string; metric: string;
  n_a: number; n_b: number;
  mean_a: number | null; mean_b: number | null;
  delta: number | null;
  pass_rate_a: number | null; pass_rate_b: number | null;
  /** 'not_tested' | 'significant' | 'not_significant' |
   *  'insufficient_data' | 'not_computable' */
  significance: string;
  p_value: number | null;
}
/** One timeline entry from GET /v1/traces/{id}/replay. The server
 *  interleaves span starts, span events, and span ends, ordered by
 *  (timestamp, span_id, kind) — a span's start precedes its events,
 *  which precede its end. `event` is only present on kind 'event' and
 *  `status` only on kind 'span_end'; `offset_ms` is relative to the
 *  earliest observed timestamp (t0 → 0). */
export interface ReplayEntry {
  t: string;
  span_id: string;
  name: string;
  kind: 'span_start' | 'span_end' | 'event';
  event?: string;
  status?: string;
  offset_ms: number;
}
/** GET /v1/traces/{id}/replay — the recorded timeline (a re-walk of
 *  stored evidence, never a re-execution). `total_ms` spans the whole
 *  timeline; `unclosed_spans` counts spans with no end_time. */
export interface ReplayTimeline {
  trace_id: string;
  entries: ReplayEntry[];
  span_count: number;
  total_ms: number;
  unclosed_spans: number;
}
/** `gate` is only present when the request carried one. */
export interface CompareGateResult {
  gate_passed: boolean;
  failures: Record<string, unknown>[];
}
/** POST /v1/eval/compare response. `multiple_comparisons` is the
 *  server's own honesty note (k unadjusted tests at alpha=0.05) —
 *  present only when more than one metric carried a p-value. */
export interface CompareReport {
  a: CompareSideResult; b: CompareSideResult;
  deltas: MetricDelta[];
  gate?: CompareGateResult;
  missing_trace_ids?: unknown;
  multiple_comparisons?: string | null;
}
/** Row of GET /v1/experiments — the summary shape the list route
 *  emits (full spec fields only come from the detail route). */
export interface ExperimentRow {
  experiment_id: string; project_id: string; name: string | null;
  status: string; result_ref: string | null; created_at: string | null;
}
/** One side of a persisted experiment spec — `label`/`config` are
 *  declared labels stored verbatim, never resolved server-side. */
export interface ExperimentCohort {
  trace_ids: string[]; label?: string | null;
  config?: Record<string, unknown>;
}
/** GET /v1/experiments/{id} — the stored spec plus the append-only
 *  run history (`runs`, paged by `run_limit`/`run_offset`). */
export interface ExperimentDetail extends ExperimentRow {
  description?: string | null;
  dataset_id?: string | null; dataset_version?: string | null;
  cohorts?: { a?: ExperimentCohort; b?: ExperimentCohort };
  evaluators?: EvalSpec[];
  provenance?: string; metadata?: Record<string, unknown>;
  runs?: ExperimentRun[]; run_limit?: number; run_offset?: number;
}
/** One row of the embedded `runs` list — `report` is the full
 *  compare_runs payload (+ `gate` when the run requested one). */
export interface ExperimentRun {
  run_id: number; experiment_id: string; project_id: string;
  run_at: string | null; report: CompareReport;
}
/** PUT /v1/experiments/{id} response. */
export interface ExperimentStored {
  experiment_id: string; project_id: string; stored: boolean;
}
/** PUT /v1/experiments/{id} request body — same spec shape as
 *  eval/compare; cohorts require 1..500 stored trace ids each. */
export interface ExperimentSpecBody {
  name?: string; description?: string;
  dataset_id?: string; dataset_version?: string;
  cohorts: { a: ExperimentCohort; b: ExperimentCohort };
  evaluators: EvalSpec[];
}
/** POST /v1/experiments/{id}/run response — the run is also appended
 *  to experiment_results server-side; `report` is the compare payload
 *  produced by that run. */
export interface ExperimentRunResponse {
  experiment_id: string; project_id: string;
  run_id: number; run_at: string; report: CompareReport;
}
/** One node of an ExecutionGraph — a span in trace graphs, a whole
 *  trace in session graphs (`kind: 'trace'`). Mirrors the backend's
 *  GraphNode: `error` is the materialized `status === 'error'` flag and
 *  `integrity_flags`/`has_token_usage` carry the stored evidence. */
export interface GraphNode {
  id: string; kind: string; name: string;
  start_time: string; end_time?: string | null;
  duration_ms?: number | null; status: string;
  integrity_flags: string[]; has_token_usage: boolean; error: boolean;
}
/** One edge — `kind` is 'parent' (containment), 'sequence' (temporal
 *  sibling succession) or 'link' (explicit SpanLink). `dangling` means
 *  the target id is not a node in this graph (cross-trace link or a
 *  parent that never arrived) — evidence, not an error. */
export interface GraphEdge {
  source: string; target: string;
  kind: 'parent' | 'sequence' | 'link' | string;
  dangling?: boolean;
}
/** GET /v1/graph/{traces,sessions}/{id} — the deterministic node/edge
 *  projection. `stats` carries span_count/edge_count/max_depth/
 *  dangling_edges; session graphs add `truncated` when the server's
 *  trace-page cap was hit. */
export interface ExecutionGraph {
  subject_id: string; project_id: string; root_id: string | null;
  nodes: GraphNode[]; edges: GraphEdge[];
  stats: Record<string, unknown>;
}
/** GET /v1/graph/traces/{id}/critical-path — `path` is the node-id
 *  chain maximizing summed durations; `cycles` lists parent edges the
 *  algorithm cut (reported as evidence). */
export interface CriticalPath {
  path: string[]; total_ms: number;
  cycles: { from: string; to: string }[];
}
/** One row of GET /v1/graph/traces/{id}/bottlenecks — node duration
 *  minus direct-children duration, ranked by `self_ms` desc. */
export interface Bottleneck {
  id: string; name: string; kind: string;
  self_ms: number; duration_ms: number | null;
}
/** One waste/anomaly finding — inside `IncidentBundle.waste` and the
 *  `waste` array of GET /v1/traces/{id}/cost. `estimated_usd` is null
 *  when the finding is not token-priced. */
export interface WasteFinding {
  kind: string; span_ids: string[];
  evidence: Record<string, unknown>;
  estimated_usd: number | null; detail: string;
}
/** One root-cause candidate inside `IncidentBundle.hypotheses` —
 *  `status` is 'proposed' | 'supported' | 'refuted' | 'unverifiable';
 *  the three evidence lists are the ledger a reviewer verifies by hand. */
export interface Hypothesis {
  hypothesis_id: string; label: string; symptom: string;
  supporting_evidence: string[]; contradictory_evidence: string[];
  missing_evidence: string[]; suggested_verification: string[];
  confidence: string; status: string;
}
/** One `eval_suggestions` entry inside the incident bundle — a
 *  `candidate` evaluator spec or dataset-case sketch derived from the
 *  failure category; `params_required` names the keys a reviewer must
 *  still fill (SLOs live outside the traces). */
export interface EvalSuggestion {
  kind: string; name: string; params: Record<string, unknown>;
  params_required: string[]; rationale: string;
  evidence: Record<string, unknown>; classification: string;
}
/** GET /v1/traces/{id}/incident — the full evidence handoff, returned
 *  as the bundle itself (no wrapper). `failure`/`cost`/`graph` are the
 *  same payloads as their dedicated endpoints; `export_sanitize`
 *  discloses what the export-boundary redaction touched. */
export interface IncidentBundle {
  trace_id: string; project_id: string; session_id: string | null;
  status: string; failure: Record<string, unknown>;
  hypotheses: Hypothesis[]; eval_suggestions: EvalSuggestion[];
  graph: Record<string, unknown>; cost: Record<string, unknown>;
  waste: WasteFinding[]; replay: ReplayTimeline;
  bundle_version: string;
  export_sanitize?: Record<string, unknown>;
}
/** One observation from GET /v1/traces/{id}/security — observer mode:
 *  the Lens reports risky/secret-shaped evidence, it never blocks. */
export interface SecurityObservation {
  kind: string; severity: string; span_id: string; detail: string;
}
/** GET /v1/traces/{id}/security — `counts` groups observations by kind;
 *  `mode` is always 'observer'. */
export interface SecurityReport {
  trace_id: string; observations: SecurityObservation[];
  counts: Record<string, number>; mode: string;
}
/** GET /v1/traces/{id}/efficiency — per-trace roll-up; `usage_coverage`
 *  is the honest "seen/total llm spans" string, not a ratio. */
export interface EfficiencyReport {
  trace_id: string; outcome: string; tool_calls: number;
  llm_calls: number; error_spans: number; retries_observed: number;
  input_tokens: number; output_tokens: number;
  usage_coverage: string; succeeded: boolean;
}
/** One candidate recommendation — every claim is `candidate`-classified
 *  because gains require experimental validation; optional fields carry
 *  the evidence for the specific rule that fired. */
export interface Recommendation {
  kind: string; classification: string; detail: string;
  spans?: string[]; tool?: string; failures?: number; tool_calls?: number;
}
/** GET /v1/traces/{id}/recommendations. */
export interface RecommendationReport {
  trace_id: string; recommendations: Recommendation[]; note: string;
}
/** One intra-trace correlation — `classification` is always
 *  'temporal_association'; `strength` carries the counts/ratios for the
 *  specific kind; `caveat` is the non-causation note to show verbatim. */
export interface Correlation {
  kind: string; classification: string;
  signals: { type: string; span_ids?: string[]; value?: unknown }[];
  strength: Record<string, unknown>;
  evidence_span_ids: string[]; caveat: string; detail: string;
}
/** GET /v1/traces/{id}/correlate — `signal_inventory` counts the raw
 *  inputs (tool/llm/error/eval spans, observed models/tools). */
export interface CorrelateReport {
  trace_id: string; correlations: Correlation[];
  signal_inventory: Record<string, unknown>;
  classification: string; note: string;
}
/** POST /v1/analyze/{specialist}/{trace_id} response — `findings` are
 *  the specialist's free-shape result objects; `stop_reason` reports
 *  why the run ended ('completed', 'timeout', 'token_budget_exceeded',
 *  'unknown_specialist', …). */
export interface AnalysisRun {
  run_id: string; specialist: string; stop_reason: string;
  iterations: number; tokens_observed: number; cancelled: boolean;
  findings: Record<string, unknown>[];
}

const BASE_KEY = 'lens-api-base';
const KEY_KEY = 'lens-api-key';

@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);
  /** API base — localStorage-overridable, same-origin-safe default. */
  base = signal(localStorage.getItem(BASE_KEY) ?? 'http://localhost:7777/v1');
  /** X-Forge-Lens-Key — required when the server runs key/keys auth. */
  setBase(url: string): void {
    localStorage.setItem(BASE_KEY, url); this.base.set(url);
  }
  setKey(k: string): void { localStorage.setItem(KEY_KEY, k); }
  private headers(): HttpHeaders {
    const k = localStorage.getItem(KEY_KEY);
    return k ? new HttpHeaders({ 'X-Forge-Lens-Key': k }) : new HttpHeaders();
  }

  /** GET /v1/traces — `{items, limit, offset}` envelope; the server
   *  echoes the (possibly clamped) paging window it actually used. */
  listTraces(f: TraceFilter):
    Observable<{ items: TraceRow[]; limit: number; offset: number }> {
    let p = new HttpParams().set('limit', f.limit ?? 100);
    const set = (k: string, v: string | number | undefined): void => {
      if (v !== undefined && v !== '') p = p.set(k, v);
    };
    set('status', f.status);
    set('session_id', f.session_id);
    set('tool', f.tool);
    set('q', f.q);
    set('model', f.model);
    set('agent', f.agent);
    set('host', f.host);
    set('min_total_tokens', f.min_total_tokens);
    set('project_id', f.project_id);
    set('since', f.since);
    set('until', f.until);
    set('min_duration_ms', f.min_duration_ms);
    set('max_duration_ms', f.max_duration_ms);
    set('offset', f.offset);
    return this.http.get<{ items: TraceRow[]; limit: number; offset: number }>(
      `${this.base()}/traces`, { params: p, headers: this.headers() });
  }
  listSessions(project_id?: string, limit?: number, offset?: number):
    Observable<SessionRow[]> {
    let p = new HttpParams();
    if (project_id) p = p.set('project_id', project_id);
    if (limit != null) p = p.set('limit', limit);
    if (offset != null) p = p.set('offset', offset);
    return this.http.get<{ items: SessionRow[] }>(`${this.base()}/sessions`,
      { params: p, headers: this.headers() }).pipe(map(r => r.items));
  }
  private get<T>(path: string): Observable<T> {
    return this.http.get<T>(`${this.base()}${path}`, { headers: this.headers() });
  }
  private post<T>(path: string, body: unknown): Observable<T> {
    return this.http.post<T>(`${this.base()}${path}`, body,
      { headers: this.headers() });
  }
  private put<T>(path: string, body: unknown): Observable<T> {
    return this.http.put<T>(`${this.base()}${path}`, body,
      { headers: this.headers() });
  }
  getTrace(id: string): Observable<Trace> { return this.get(`/traces/${id}`); }
  getGraphAnalysis(id: string): Observable<Record<string, unknown>> {
    return this.get(`/graph/traces/${id}/analysis`);
  }
  /** GET /v1/graph/traces/{id} — the span-level ExecutionGraph. */
  getTraceGraph(id: string): Observable<ExecutionGraph> {
    return this.get(`/graph/traces/${id}`);
  }
  /** GET /v1/graph/sessions/{id} — the session-level graph whose nodes
   *  are traces; `stats.truncated` flags a server-side page cap. */
  getSessionGraph(id: string): Observable<ExecutionGraph> {
    return this.get(`/graph/sessions/${id}`);
  }
  /** GET /v1/graph/traces/{id}/critical-path — node-id chain only. */
  getCriticalPath(id: string): Observable<CriticalPath> {
    return this.get(`/graph/traces/${id}/critical-path`);
  }
  /** GET /v1/graph/traces/{id}/bottlenecks?top=n — self-time ranking
   *  (`top` is clamped 0..100 server-side). */
  getBottlenecks(id: string, top = 10): Observable<{ items: Bottleneck[] }> {
    return this.http.get<{ items: Bottleneck[] }>(
      `${this.base()}/graph/traces/${id}/bottlenecks`,
      { params: new HttpParams().set('top', top), headers: this.headers() });
  }
  getCost(id: string): Observable<Record<string, unknown>> {
    return this.get(`/traces/${id}/cost`);
  }
  getFailure(id: string): Observable<Record<string, unknown>> {
    return this.get(`/traces/${id}/failure`);
  }
  /** GET /v1/traces/{id}/replay — ordered timeline entries; not the
   *  SSE `/stream` route. */
  getReplay(id: string): Observable<ReplayTimeline> {
    return this.get(`/traces/${id}/replay`);
  }
  getLoops(id: string): Observable<Loops> { return this.get(`/traces/${id}/loops`); }
  getTraceEval(id: string): Observable<TraceEval> {
    return this.get(`/traces/${id}/eval`);
  }
  /** GET /v1/traces/{id}/incident — the whole evidence bundle
   *  (failure + hypotheses + eval_suggestions + graph + cost + waste +
   *  replay) in one call; the payload is already redact-exported. */
  getIncident(id: string): Observable<IncidentBundle> {
    return this.get(`/traces/${id}/incident`);
  }
  /** GET /v1/traces/{id}/security — MCP/tool-call observations
   *  (observer mode, sorted high-severity first). */
  getSecurityReport(id: string): Observable<SecurityReport> {
    return this.get(`/traces/${id}/security`);
  }
  /** GET /v1/traces/{id}/efficiency — per-trace tokens/tools/retries
   *  roll-up; missing signals stay honest strings/zero counts. */
  getEfficiency(id: string): Observable<EfficiencyReport> {
    return this.get(`/traces/${id}/efficiency`);
  }
  /** GET /v1/traces/{id}/recommendations — `candidate` suggestions
   *  only; gains are never claimed server-side. */
  getRecommendations(id: string): Observable<RecommendationReport> {
    return this.get(`/traces/${id}/recommendations`);
  }
  /** GET /v1/traces/{id}/correlate — intra-trace temporal associations;
   *  every row is non-causal evidence with its span ids. */
  getCorrelate(id: string): Observable<CorrelateReport> {
    return this.get(`/traces/${id}/correlate`);
  }
  /** GET /v1/analyze/specialists — registered specialist names,
   *  already sorted server-side. */
  listSpecialists(): Observable<string[]> {
    return this.get<{ specialists: string[] }>(`/analyze/specialists`)
      .pipe(map(r => r.specialists ?? []));
  }
  /** POST /v1/analyze/{specialist}/{trace_id} — runs one specialist
   *  under server-side default budgets; the wire body is `{}` (all
   *  fields optional; `budget`/`context`/`project_id`/`run_id` can be
   *  passed through `body` when a caller needs them). */
  runAnalysis(traceId: string, specialist: string,
    body?: Record<string, unknown>): Observable<AnalysisRun> {
    return this.post(`/analyze/${specialist}/${traceId}`, body ?? {});
  }
  /** `evaluators` are `{name, params?}` spec objects — posted verbatim;
   *  the wire field is `trace_ids` (a list), per POST /v1/eval/run. */
  runEval(traceId: string, evaluators: EvalSpec[]): Observable<EvalReport> {
    return this.post('/eval/run', {
      trace_ids: [traceId],
      evaluators,
    });
  }
  /** GET /v1/datasets — bare array, project-scoped by the server when
   *  `project_id` is omitted. */
  listDatasets(project_id?: string): Observable<DatasetRow[]> {
    let p = new HttpParams();
    if (project_id) p = p.set('project_id', project_id);
    return this.http.get<DatasetRow[]>(`${this.base()}/datasets`,
      { params: p, headers: this.headers() });
  }
  getDataset(id: string): Observable<DatasetDetail> {
    return this.get(`/datasets/${id}`);
  }
  listDatasetVersions(id: string): Observable<DatasetVersionRow[]> {
    return this.get(`/datasets/${id}/versions`);
  }
  /** GET /v1/datasets/{id}/versions/{v} — the stored version with its
   *  cases ordered by case_id. */
  getDatasetVersion(id: string, version: string):
    Observable<DatasetVersionDetail> {
    return this.get(`/datasets/${id}/versions/${version}`);
  }
  /** PUT /v1/datasets/{id} — upserts the mutable metadata (name/task);
   *  `task` omitted entirely when the field is left blank. */
  putDataset(id: string, body: { name: string; task?: string }):
    Observable<DatasetStored> {
    return this.put(`/datasets/${id}`, body);
  }
  /** PUT /v1/datasets/{id}/versions/{v} — append-only; a repeat of an
   *  existing (dataset, version) pair is a server-side 409. */
  putDatasetVersion(id: string, version: string,
    body: { cases: DatasetCaseInput[]; description?: string }):
    Observable<DatasetVersionStored> {
    return this.put(`/datasets/${id}/versions/${version}`, body);
  }
  /** POST /v1/eval/compare — both cohorts carry stored `trace_ids`;
   *  a per-side `dataset` string is a report label, not a case lookup. */
  runCompare(body: CompareRequest): Observable<CompareReport> {
    return this.post('/eval/compare', body);
  }
  /** POST /v1/eval/run over an arbitrary id set — per-trace results
   *  keyed by trace_id (used to surface failing examples a compare
   *  report only aggregates). */
  runEvalSet(traceIds: string[], evaluators: EvalSpec[]):
    Observable<EvalReport> {
    return this.post('/eval/run', { trace_ids: traceIds, evaluators });
  }
  /** GET /v1/experiments — `{items, limit, offset}` envelope of
   *  summary rows, project-scoped server-side. */
  listExperiments(limit = 500, offset = 0):
    Observable<{ items: ExperimentRow[]; limit: number; offset: number }> {
    const p = new HttpParams().set('limit', limit).set('offset', offset);
    return this.http.get<{ items: ExperimentRow[]; limit: number; offset: number }>(
      `${this.base()}/experiments`, { params: p, headers: this.headers() });
  }
  /** GET /v1/experiments/{id} — spec + append-only run history. */
  getExperiment(id: string): Observable<ExperimentDetail> {
    return this.get(`/experiments/${id}`);
  }
  /** PUT /v1/experiments/{id} — upserts the spec; prior runs stay
   *  pinned in experiment_results (append-only). */
  putExperiment(id: string, body: ExperimentSpecBody):
    Observable<ExperimentStored> {
    return this.put(`/experiments/${id}`, body);
  }
  /** POST /v1/experiments/{id}/run — executes the stored spec through
   *  the same compare path; body is `{gate?}` (JSON object required). */
  runExperiment(id: string, gate?: Record<string, unknown>):
    Observable<ExperimentRunResponse> {
    return this.post(`/experiments/${id}/run`, gate ? { gate } : {});
  }
  getOps(): Observable<OpsSummary> { return this.get(`/ops/summary`); }
  /** POST /v1/alerts/evaluate — `{use_saved: true}` merges the rules
   *  persisted via POST /v1/alerts/rules into the evaluation; metrics
   *  are store-computed at request time. */
  evalAlerts(): Observable<AlertEval> {
    return this.post('/alerts/evaluate', { use_saved: true });
  }
  getCapabilities(): Observable<Capabilities> { return this.get(`/capabilities`); }
}
