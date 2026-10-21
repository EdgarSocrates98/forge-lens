import { Injectable, inject, signal } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { Observable } from 'rxjs';

export interface TraceRow {
  trace_id: string; name?: string; status?: string;
  started_at?: string; ended_at?: string; span_count?: number;
  [k: string]: unknown;
}
export interface Span {
  span_id: string; parent_span_id?: string | null; name: string;
  kind: string; start_time: string; end_time?: string | null;
  duration_ms?: number | null; status: { code: string; message?: string };
  attributes?: Record<string, unknown>;
}
export interface Trace {
  trace_id: string; spans: Span[]; status: { code: string };
  [k: string]: unknown;
}

@Injectable({ providedIn: 'root' })
export class Api {
  private http = inject(HttpClient);
  base = signal('http://localhost:7777/v1');

  listTraces(f: { status?: string; q?: string; limit?: number }): Observable<{ items: TraceRow[] }> {
    let p = new HttpParams();
    if (f.status) p = p.set('status', f.status);
    p = p.set('limit', f.limit ?? 100);
    return this.http.get<{ items: TraceRow[] }>(`${this.base()}/traces`, { params: p });
  }
  getTrace(id: string): Observable<Trace> {
    return this.http.get<Trace>(`${this.base()}/traces/${id}`);
  }
  getGraph(id: string): Observable<unknown> {
    return this.http.get(`${this.base()}/graph/traces/${id}`);
  }
  getGraphAnalysis(id: string): Observable<unknown> {
    return this.http.get(`${this.base()}/graph/traces/${id}/analysis`);
  }
  getCost(id: string): Observable<unknown> {
    return this.http.get(`${this.base()}/traces/${id}/cost`);
  }
  getFailure(id: string): Observable<unknown> {
    return this.http.get(`${this.base()}/traces/${id}/failure`);
  }
  getLoops(id: string): Observable<unknown> {
    return this.http.get(`${this.base()}/traces/${id}/loops`);
  }
  getOps(): Observable<unknown> {
    return this.http.get(`${this.base()}/ops/summary`);
  }
  getCapabilities(): Observable<unknown> {
    return this.http.get(`${this.base()}/capabilities`);
  }
}
