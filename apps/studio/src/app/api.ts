import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpParams, HttpHeaders } from '@angular/common/http';
import { Observable } from 'rxjs';

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

  listTraces(f: { status?: string; limit?: number }): Observable<{ items: TraceRow[] }> {
    let p = new HttpParams();
    if (f.status) p = p.set('status', f.status);
    p = p.set('limit', f.limit ?? 100);
    return this.http.get<{ items: TraceRow[] }>(`${this.base()}/traces`,
      { params: p, headers: this.headers() });
  }
  private get<T>(path: string): Observable<T> {
    return this.http.get<T>(`${this.base()}${path}`, { headers: this.headers() });
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
  getOps(): Observable<OpsSummary> { return this.get(`/ops/summary`); }
  getCapabilities(): Observable<Capabilities> { return this.get(`/capabilities`); }
}
