import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink, ActivatedRoute, Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, TraceRow } from './api';

type SortKey = 'started_at' | 'status' | 'span_count';

@Component({
  selector: 'lens-traces',
  imports: [RouterLink, FormsModule, DatePipe],
  template: `
  <h1>Trace Explorer</h1>
  <form class="filters" (ngSubmit)="load()">
    <select [(ngModel)]="status" name="status" aria-label="status filter">
      <option value="">all statuses</option>
      <option value="ok">ok</option><option value="error">error</option>
      <option value="unset">unset</option>
    </select>
    <input [(ngModel)]="limit" name="limit" type="number" min="1" max="500"
           aria-label="row limit" />
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
  }`,
})
export class TracesComponent implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  rows = signal<TraceRow[]>([]);
  sorted = signal<TraceRow[]>([]);
  status = '';
  limit = 100;
  loaded = signal(false);
  error = signal<string | null>(null);
  private sortKey: SortKey = 'started_at';
  shown = signal(200);
  views = signal<string[]>([]);
  viewSel = '';

  ngOnInit(): void {
    this.status = this.route.snapshot.queryParamMap.get('status') ?? '';
    try {
      this.views.set(JSON.parse(localStorage.getItem('lens-views') ?? '[]') as string[]);
    } catch { this.views.set([]); }
    this.load();
  }
  load(): void {
    void this.router.navigate([], {
      queryParams: { status: this.status || null }, queryParamsHandling: 'merge',
    });
    this.error.set(null); this.loaded.set(false);
    this.api.listTraces({ status: this.status || undefined, limit: this.limit })
      .subscribe({
        next: r => { this.rows.set(r.items); this.applySort(); this.loaded.set(true); },
        error: (e: HttpErrorResponse) => {
          this.error.set(e.status === 401 || e.status === 403
            ? `HTTP ${e.status} — set an API key (localStorage 'lens-api-key')`
            : `API unreachable (${e.status || e.message}) — is forge-lens serve running?`);
          this.loaded.set(true);
        },
      });
  }
  sortBy(k: SortKey): void { this.sortKey = k; this.applySort(); }
  visible = () => this.sorted().slice(0, this.shown());
  showMore(): void { this.shown.update(n => n + 500); }
  saveView(): void {
    const name = `status=${this.status || '*'};limit=${this.limit}`;
    const all = new Set(this.views()); all.add(name);
    this.views.set([...all].sort());
    localStorage.setItem('lens-views', JSON.stringify(this.views()));
    localStorage.setItem(`lens-view:${name}`,
      JSON.stringify({ status: this.status, limit: this.limit }));
  }
  applyView(): void {
    const raw = localStorage.getItem(`lens-view:${this.viewSel}`);
    if (!raw) return;
    try {
      const v = JSON.parse(raw) as { status: string; limit: number };
      this.status = v.status ?? ''; this.limit = v.limit || 100;
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
  private applySort(): void {
    const k = this.sortKey;
    const num = k === 'span_count';
    this.sorted.set([...this.rows()].sort((a, b) => num
      ? Number(b[k] ?? 0) - Number(a[k] ?? 0)
      : String(b[k] ?? '').localeCompare(String(a[k] ?? ''))));
  }
}
