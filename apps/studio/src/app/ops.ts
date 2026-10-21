import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { AlertEval, Api, OpsSummary } from './api';

interface StatCard { label: string; value: string; }

/** stats keys the SQLite store emits today, in display order — any
 *  other key a store adds still renders (sorted after these). */
const STAT_DEFS: { key: string; label: string; bytes?: boolean }[] = [
  { key: 'traces', label: 'traces' },
  { key: 'spans', label: 'spans' },
  { key: 'error_spans', label: 'error spans' },
  { key: 'db_size_bytes', label: 'db size', bytes: true },
  { key: 'schema_version', label: 'schema' },
];

const fmtBytes = (n: number): string => {
  if (n < 1024) return `${n} B`;
  const units = ['KiB', 'MiB', 'GiB', 'TiB'];
  let v = n, i = -1;
  do { v /= 1024; i++; } while (v >= 1024 && i < units.length - 1);
  return `${v.toFixed(1)} ${units[i]}`;
};

@Component({
  selector: 'lens-ops',
  imports: [RouterLink],
  styles: [`
    .stat-grid { display: grid; gap: .6rem; margin: .8rem 0;
      grid-template-columns: repeat(auto-fill, minmax(8.5rem, 1fr)); }
    .stat { background: var(--panel); border-radius: 6px; padding: .6rem .8rem; }
    .stat b { display: block; font-size: 1.25rem; font-weight: 600; }
    .stat span { opacity: .6; font-size: .85em; }
  `],
  template: `
  <h1>Operations</h1>
  <div class="filters" style="align-items:center">
    <button (click)="load()" [disabled]="refreshing()">
      {{ refreshing() ? 'refreshing…' : 'refresh' }}</button>
    @if (ops(); as o) {
      <span class="muted">project {{ o.project_id ?? '(all)' }}</span>
    }
  </div>

  @if (opsErr()) { <p class="err-banner">{{ opsErr() }}</p> }
  @else if (ops(); as o) {
    <div class="stat-grid">
      @for (s of statCards(o); track s.label) {
        <div class="stat"><b>{{ s.value }}</b><span>{{ s.label }}</span></div>
      }
    </div>
    <p class="muted">{{ o.note }}</p>

    <h3>recent failures</h3>
    @if (o.recent_failures.length === 0) {
      <p class="muted">no failed traces in the bounded window.</p>
    } @else {
      <table>
        <thead><tr><th>trace</th><th>status</th></tr></thead>
        <tbody>
          @for (id of o.recent_failures; track id) {
            <tr class="err">
              <td><a [routerLink]="['/traces', id]">{{ id }}</a>
                <button class="mini" (click)="copyId(id)" title="copy id">⧉</button></td>
              <td>error</td>
            </tr>
          }
        </tbody>
      </table>
    }
  } @else { <p class="muted">loading…</p> }

  <h3>alerts</h3>
  @if (alertErr()) { <p class="err-banner">{{ alertErr() }}</p> }
  @else if (alerts(); as a) {
    <p class="muted">{{ a.evaluated }} saved rule(s) evaluated ·
      {{ a.fired.length }} fired · project {{ a.project_id ?? '(all)' }}
      — rules persist via POST /v1/alerts/rules.</p>
    @if (a.fired.length > 0) {
      <table>
        <thead><tr>
          <th>rule</th><th>metric</th><th>op</th><th>threshold</th>
          <th>observed</th><th>severity</th><th>status</th><th>message</th>
        </tr></thead>
        <tbody>
          @for (f of a.fired; track f.rule_id) {
            <tr>
              <td>{{ f.rule_id }}</td><td>{{ f.metric }}</td>
              <td>{{ f.op }}</td><td>{{ f.threshold }}</td>
              <td>{{ f.observed }}</td><td>{{ f.severity }}</td>
              <td><b class="error">fired</b></td><td>{{ f.message }}</td>
            </tr>
          }
        </tbody>
      </table>
    } @else if (a.evaluated > 0) {
      <p>all {{ a.evaluated }} evaluated rule(s) passed — none fired.</p>
    } @else {
      <p class="muted">no saved rules — persist one via
        POST /v1/alerts/rules.</p>
    }
  } @else { <p class="muted">loading…</p> }`,
})
export class OpsComponent implements OnInit {
  private api = inject(Api);
  ops = signal<OpsSummary | null>(null);
  alerts = signal<AlertEval | null>(null);
  opsErr = signal<string | null>(null);
  alertErr = signal<string | null>(null);
  refreshing = signal(false);
  private pending = 0;

  ngOnInit(): void { this.load(); }

  /** manual refresh only — no polling; re-issues both calls. */
  load(): void {
    this.refreshing.set(true);
    this.opsErr.set(null); this.alertErr.set(null);
    this.pending = 2;
    this.api.getOps().subscribe({
      next: o => { this.ops.set(o); this.done(); },
      error: (e: HttpErrorResponse) => {
        this.opsErr.set(this.httpErr(e, 'ops summary unavailable'));
        this.done();
      },
    });
    this.api.evalAlerts().subscribe({
      next: a => { this.alerts.set(a); this.done(); },
      error: (e: HttpErrorResponse) => {
        this.alertErr.set(this.httpErr(e, 'alerts unavailable'));
        this.done();
      },
    });
  }

  /** store stats → cards: known keys labelled first, the bounded-window
   *  counts appended, then any extra keys a store emits (sorted). */
  statCards(o: OpsSummary): StatCard[] {
    const stats = o.stats ?? {};
    const cards: StatCard[] = [];
    const seen = new Set<string>();
    for (const d of STAT_DEFS) {
      seen.add(d.key);
      if (d.key in stats) {
        cards.push({ label: d.label, value: this.fmtStat(stats[d.key], d.bytes) });
      }
    }
    for (const k of Object.keys(stats).sort()) {
      if (!seen.has(k)) cards.push({ label: k, value: this.fmtStat(stats[k]) });
    }
    cards.push({ label: 'traces in window', value: String(o.recent_traces) });
    cards.push({ label: 'failed in window', value: String(o.recent_failures.length) });
    return cards;
  }

  copyId(id: string): void { void navigator.clipboard.writeText(id); }

  /** scalar → text; null/absent → '—' (never a fabricated zero);
   *  objects render as JSON so nothing is silently dropped. */
  private fmtStat(v: unknown, bytes = false): string {
    if (v == null) return '—';
    if (typeof v === 'number') {
      return Number.isFinite(v) ? (bytes ? fmtBytes(v) : String(v)) : '—';
    }
    if (typeof v === 'string' || typeof v === 'boolean') return String(v);
    return JSON.stringify(v);
  }

  private done(): void {
    if (--this.pending <= 0) this.refreshing.set(false);
  }

  /** same failure vocabulary as the traces view — auth errors get the
   *  key hint, everything else the server-down hint. */
  private httpErr(e: HttpErrorResponse, what: string): string {
    if (e.status === 401 || e.status === 403) {
      return `HTTP ${e.status} — set an API key (localStorage 'lens-api-key')`;
    }
    return `${what} (${e.status || e.message}) — is forge-lens serve running?`;
  }
}
