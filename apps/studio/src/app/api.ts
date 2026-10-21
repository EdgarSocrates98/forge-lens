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
/** Server-side filters accepted by GET /v1/traces — empty/undefined omitted. */
export interface TraceFilter {
  status?: string; session_id?: string; tool?: string; q?: string;
  since?: string; until?: string;
  min_duration_ms?: number; max_duration_ms?: number;
  limit?: number;
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
/** `gate` is only present when the request carried one. */
export interface CompareGateResult {
  gate_passed: boolean;
  failures: Record<string, unknown>[];
}
/** POST /v1/eval/compare response. */
export interface CompareReport {
  a: CompareSideResult; b: CompareSideResult;
  deltas: MetricDelta[];
  gate?: CompareGateResult;
  missing_trace_ids?: unknown;
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

  listTraces(f: TraceFilter): Observable<{ items: TraceRow[] }> {
    let p = new HttpParams().set('limit', f.limit ?? 100);
    const set = (k: string, v: string | number | undefined): void => {
      if (v !== undefined && v !== '') p = p.set(k, v);
    };
    set('status', f.status);
    set('session_id', f.session_id);
    set('tool', f.tool);
    set('q', f.q);
    set('since', f.since);
    set('until', f.until);
    set('min_duration_ms', f.min_duration_ms);
    set('max_duration_ms', f.max_duration_ms);
    return this.http.get<{ items: TraceRow[] }>(`${this.base()}/traces`,
      { params: p, headers: this.headers() });
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
  getCost(id: string): Observable<Record<string, unknown>> {
    return this.get(`/traces/${id}/cost`);
  }
  getFailure(id: string): Observable<Record<string, unknown>> {
    return this.get(`/traces/${id}/failure`);
  }
  getLoops(id: string): Observable<Loops> { return this.get(`/traces/${id}/loops`); }
  getTraceEval(id: string): Observable<TraceEval> {
    return this.get(`/traces/${id}/eval`);
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
  getOps(): Observable<OpsSummary> { return this.get(`/ops/summary`); }
  getCapabilities(): Observable<Capabilities> { return this.get(`/capabilities`); }
}
