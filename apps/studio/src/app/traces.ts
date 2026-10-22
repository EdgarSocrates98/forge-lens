import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink, ActivatedRoute, Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, TraceRow } from './api';

type SortKey = 'started_at' | 'status' | 'span_count';

/** defensive coercion for saved-view / query-param payloads */
const strOf = (v: unknown): string => typeof v === 'string' ? v : '';
const numOf = (v: unknown): number | null =>
  typeof v === 'number' && Number.isFinite(v) ? v : null;
const numParam = (v: string | null): number | null => {
  if (v == null || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
};

@Component({
  selector: 'lens-traces',
  imports: [RouterLink, FormsModule, DatePipe],
  template: `
  <h1>Trace Explorer</h1>
  <form class="filters" style="flex-wrap:wrap" (ngSubmit)="apply()">
    <select [(ngModel)]="status" name="status" aria-label="status filter">
      <option value="">all statuses</option>
      <option value="ok">ok</option><option value="error">error</option>
      <option value="unset">unset</option>
    </select>
    <input [(ngModel)]="q" name="q" type="search" size="14"
           placeholder="trace id / span name" aria-label="text search" />
    <input [(ngModel)]="tool" name="tool" size="9"
           placeholder="tool name" aria-label="tool filter" />
    <input [(ngModel)]="sessionId" name="session_id" size="14"
           placeholder="session id" aria-label="session id filter" />
    <input [(ngModel)]="model" name="model" size="9"
           placeholder="model" aria-label="model filter" />
    <input [(ngModel)]="agent" name="agent" size="9"
           placeholder="agent" aria-label="agent filter" />
    <input [(ngModel)]="host" name="host" size="9"
           placeholder="host" aria-label="host filter" />
    <input [(ngModel)]="minTotalTokens" name="min_total_tokens" type="number" min="0"
           placeholder="min tokens" aria-label="min total tokens" style="width:6.5rem" />
    <input [(ngModel)]="projectId" name="project_id" size="10"
           placeholder="project" aria-label="project id filter" />
    <input [(ngModel)]="minDuration" name="min_duration_ms" type="number" min="0"
           placeholder="min ms" aria-label="min duration ms" style="width:6.5rem" />
    <input [(ngModel)]="maxDuration" name="max_duration_ms" type="number" min="0"
           placeholder="max ms" aria-label="max duration ms" style="width:6.5rem" />
    <input [(ngModel)]="since" name="since" type="datetime-local"
           aria-label="since (local time)" title="since (local time)" />
    <input [(ngModel)]="until" name="until" type="datetime-local"
           aria-label="until (local time)" title="until (local time)" />
    <input [(ngModel)]="limit" name="limit" type="number" min="1" max="500"
           aria-label="row limit" style="width:5rem" />
    <button type="submit">Apply</button>
    <button type="button" (click)="saveView()">Save view</button>
    <select [(ngModel)]="viewSel" name="view" (change)="applyView()"
            aria-label="saved views">
      <option value="">saved views…</option>
      @for (v of views(); track v) { <option [value]="v">{{ v }}</option> }
    </select>
    <button type="button" (click)="exportJson()">Export JSON</button>
  </form>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (rows().length === 0 && loaded()) {
    <p class="muted">no traces — instrument a run or check the API base.</p> }
  <table>
    <thead><tr>
      <th (click)="sortBy('started_at')">started</th>
      <th>trace</th><th (click)="sortBy('status')">status</th>
      <th (click)="sortBy('span_count')">spans</th>
    </tr></thead>
    <tbody>
      @for (t of visible(); track t.trace_id) {
        <tr [class.err]="t.status === 'error'">
          <td>{{ t.started_at | date:'HH:mm:ss.SSS' }}</td>
          <td><a [routerLink]="['/traces', t.trace_id]">{{ t.trace_id }}</a>
            <button class="mini" (click)="copyId(t.trace_id)" title="copy id">⧉</button></td>
          <td>{{ t.status }}</td><td>{{ t.span_count }}</td>
        </tr>
      }
    </tbody>
  </table>
  @if (sorted().length > shown()) {
    <button (click)="showMore()">show more ({{ sorted().length - shown() }} hidden)</button>
  }
  @if (loading()) { <p class="muted">loading…</p> }
  @else if (hasMore()) {
    <button (click)="loadMore()">load more — next {{ limit }} from server</button>
  }`,
})
export class TracesComponent implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  rows = signal<TraceRow[]>([]);
  sorted = signal<TraceRow[]>([]);
  status = '';
  q = '';
  tool = '';
  sessionId = '';
  model = '';
  agent = '';
  host = '';
  minTotalTokens: number | null = null;
  projectId = '';
  minDuration: number | null = null;
  maxDuration: number | null = null;
  since = '';  // datetime-local field values — converted to ISO on the wire
  until = '';
  limit = 100;
  /** next server-side offset to fetch — hydrated once from ?offset=,
   *  reset to 0 on every filter apply (same contract as sessions). */
  private offset = 0;
  hasMore = signal(false);
  loading = signal(false);
  loaded = signal(false);
  error = signal<string | null>(null);
  private sortKey: SortKey = 'started_at';
  shown = signal(200);
  views = signal<string[]>([]);
  viewSel = '';

  ngOnInit(): void {
    const pm = this.route.snapshot.queryParamMap;
    this.status = pm.get('status') ?? '';
    this.q = pm.get('q') ?? '';
    this.tool = pm.get('tool') ?? '';
    this.sessionId = pm.get('session_id') ?? '';
    this.model = pm.get('model') ?? '';
    this.agent = pm.get('agent') ?? '';
    this.host = pm.get('host') ?? '';
    this.minTotalTokens = numParam(pm.get('min_total_tokens'));
    this.projectId = pm.get('project_id') ?? '';
    this.minDuration = numParam(pm.get('min_duration_ms'));
    this.maxDuration = numParam(pm.get('max_duration_ms'));
    this.since = this.toLocalInput(pm.get('since'));
    this.until = this.toLocalInput(pm.get('until'));
    const lim = numParam(pm.get('limit'));
    if (lim != null && lim > 0) this.limit = lim;
    const off = numParam(pm.get('offset'));
    if (off != null && off >= 0) this.offset = off;
    try {
      this.views.set(JSON.parse(localStorage.getItem('lens-views') ?? '[]') as string[]);
    } catch { this.views.set([]); }
    this.load();
  }
  /** Form submit — a new filter set always restarts paging at 0. */
  apply(): void { this.offset = 0; this.load(); }
  load(): void {
    void this.router.navigate([], {
      queryParams: {
        status: this.status || null,
        q: this.q || null,
        tool: this.tool || null,
        session_id: this.sessionId || null,
        model: this.model || null,
        agent: this.agent || null,
        host: this.host || null,
        min_total_tokens: this.minTotalTokens ?? null,
        project_id: this.projectId || null,
        min_duration_ms: this.minDuration ?? null,
        max_duration_ms: this.maxDuration ?? null,
        since: this.isoOf(this.since) ?? null,
        until: this.isoOf(this.until) ?? null,
        limit: this.limit,
        offset: this.offset || null,
      },
      queryParamsHandling: 'merge',
    });
    this.error.set(null); this.loaded.set(false); this.hasMore.set(false);
    this.fetch(this.offset);
  }
  loadMore(): void {
    if (this.loading() || !this.hasMore()) return;
    this.loading.set(true);
    this.fetch(this.offset);
  }
  private fetch(offset: number): void {
    this.api.listTraces({
      status: this.status || undefined,
      q: this.q || undefined,
      tool: this.tool || undefined,
      session_id: this.sessionId || undefined,
      model: this.model || undefined,
      agent: this.agent || undefined,
      host: this.host || undefined,
      min_total_tokens: this.minTotalTokens ?? undefined,
      project_id: this.projectId || undefined,
      since: this.isoOf(this.since),
      until: this.isoOf(this.until),
      min_duration_ms: this.minDuration ?? undefined,
      max_duration_ms: this.maxDuration ?? undefined,
      limit: this.limit,
      offset,
    }).subscribe({
      next: r => {
        this.rows.update(rs => {
          if (offset === 0) return r.items;
          // drop stragglers already seen — ordering may shift under a live ingest
          const seen = new Set(rs.map(t => t.trace_id));
          return [...rs, ...r.items.filter(t => !seen.has(t.trace_id))];
        });
        this.applySort();
        this.offset = offset + r.items.length;
        this.hasMore.set(r.items.length === this.limit);
        this.loading.set(false); this.loaded.set(true);
      },
      error: (e: HttpErrorResponse) => {
        this.error.set(e.status === 401 || e.status === 403
          ? `HTTP ${e.status} — set an API key (localStorage 'lens-api-key')`
          : `API unreachable (${e.status || e.message}) — is forge-lens serve running?`);
        this.loading.set(false); this.loaded.set(true);
      },
    });
  }
  sortBy(k: SortKey): void { this.sortKey = k; this.applySort(); }
  visible = () => this.sorted().slice(0, this.shown());
  showMore(): void { this.shown.update(n => n + 500); }
  saveView(): void {
    const name = this.viewName();
    const all = new Set(this.views()); all.add(name);
    this.views.set([...all].sort());
    localStorage.setItem('lens-views', JSON.stringify(this.views()));
    localStorage.setItem(`lens-view:${name}`, JSON.stringify({
      status: this.status, limit: this.limit, q: this.q, tool: this.tool,
      session_id: this.sessionId,
      model: this.model, agent: this.agent, host: this.host,
      min_total_tokens: this.minTotalTokens, project_id: this.projectId,
      min_duration_ms: this.minDuration, max_duration_ms: this.maxDuration,
      since: this.since, until: this.until,
    }));
  }
  applyView(): void {
    const raw = localStorage.getItem(`lens-view:${this.viewSel}`);
    if (!raw) return;
    try {
      const v = JSON.parse(raw) as Record<string, unknown>;
      this.status = strOf(v['status']);
      this.q = strOf(v['q']);
      this.tool = strOf(v['tool']);
      this.sessionId = strOf(v['session_id']);
      this.model = strOf(v['model']);
      this.agent = strOf(v['agent']);
      this.host = strOf(v['host']);
      this.minTotalTokens = numOf(v['min_total_tokens']);
      this.projectId = strOf(v['project_id']);
      this.limit = numOf(v['limit']) ?? 100;
      this.minDuration = numOf(v['min_duration_ms']);
      this.maxDuration = numOf(v['max_duration_ms']);
      this.since = this.toLocalInput(strOf(v['since']));
      this.until = this.toLocalInput(strOf(v['until']));
      this.offset = 0;   // a saved view always replays from page 1
      this.load();
    } catch {
      // corrupted saved view — ignore rather than crash the list
      localStorage.removeItem(`lens-view:${this.viewSel}`);
    }
  }
  exportJson(): void {
    const blob = new Blob([JSON.stringify(this.sorted(), null, 2)],
      { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'traces.json'; a.click();
    URL.revokeObjectURL(a.href);
  }
  copyId(id: string): void { void navigator.clipboard.writeText(id); }
  private viewName(): string {
    const p = [`status=${this.status || '*'}`, `limit=${this.limit}`];
    if (this.q) p.push(`q=${this.q}`);
    if (this.tool) p.push(`tool=${this.tool}`);
    if (this.sessionId) p.push(`session=${this.sessionId}`);
    if (this.model) p.push(`model=${this.model}`);
    if (this.agent) p.push(`agent=${this.agent}`);
    if (this.host) p.push(`host=${this.host}`);
    if (this.minTotalTokens != null) p.push(`minTok=${this.minTotalTokens}`);
    if (this.projectId) p.push(`project=${this.projectId}`);
    if (this.minDuration != null) p.push(`min=${this.minDuration}ms`);
    if (this.maxDuration != null) p.push(`max=${this.maxDuration}ms`);
    if (this.since) p.push(`since=${this.since}`);
    if (this.until) p.push(`until=${this.until}`);
    return p.join(';');
  }
  /** datetime-local field value → ISO instant for URL + API (undefined if empty/invalid). */
  private isoOf(local: string): string | undefined {
    if (!local) return undefined;
    const t = Date.parse(local);
    return Number.isNaN(t) ? undefined : new Date(t).toISOString();
  }
  /** any parseable date string → 'yyyy-MM-ddTHH:mm' local input value ('' if empty/invalid). */
  private toLocalInput(v: string | null): string {
    if (!v) return '';
    const t = Date.parse(v);
    if (Number.isNaN(t)) return '';
    const d = new Date(t);
    const p = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
      `T${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  private applySort(): void {
    const k = this.sortKey;
    const num = k === 'span_count';
    this.sorted.set([...this.rows()].sort((a, b) => num
      ? Number(b[k] ?? 0) - Number(a[k] ?? 0)
      : String(b[k] ?? '').localeCompare(String(a[k] ?? ''))));
  }
}
