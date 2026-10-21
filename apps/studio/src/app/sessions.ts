import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, SessionRow } from './api';

@Component({
  selector: 'lens-sessions',
  imports: [RouterLink, FormsModule, DatePipe],
  template: `
  <h1>Sessions</h1>
  <div class="filters">
    <input [ngModel]="search()" (ngModelChange)="search.set($event)"
           name="search" type="search" size="28"
           [ngModelOptions]="{ standalone: true }"
           placeholder="filter by session id prefix…" aria-label="filter sessions" />
  </div>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (loaded() && filtered().length === 0) {
    <p class="muted">no sessions — ingest traces carrying a session id.</p> }
  <table>
    <thead><tr>
      <th>session</th><th>traces</th><th>spans</th>
      <th>first started</th><th>last activity</th>
    </tr></thead>
    <tbody>
      @for (s of filtered(); track s.session_id) {
        <tr>
          <td><a [routerLink]="['/traces']"
                 [queryParams]="{ session_id: s.session_id }"
                 [title]="s.session_id">{{ short(s.session_id) }}</a>
            <button class="mini" (click)="copyId(s.session_id)" title="copy id">⧉</button></td>
          <td>{{ s.traces }}</td><td>{{ s.spans }}</td>
          <td>{{ s.first_started_at | date:'yyyy-MM-dd HH:mm:ss' }}</td>
          <td>{{ s.last_activity_at
               ? (s.last_activity_at | date:'yyyy-MM-dd HH:mm:ss') : '—' }}</td>
        </tr>
      }
    </tbody>
  </table>
  @if (loading()) { <p class="muted">loading…</p> }
  @else if (hasMore()) { <button (click)="loadMore()">load more</button> }`,
})
export class SessionsComponent implements OnInit {
  private api = inject(Api);
  rows = signal<SessionRow[]>([]);
  search = signal('');
  loaded = signal(false);
  loading = signal(false);
  hasMore = signal(false);
  error = signal<string | null>(null);
  readonly pageSize = 50;
  private offset = 0;

  /** client-side prefix filter over the pages fetched so far */
  filtered = computed(() => {
    const p = this.search().trim();
    const all = this.rows();
    return p ? all.filter(r => r.session_id.startsWith(p)) : all;
  });

  ngOnInit(): void { this.fetch(0); }
  loadMore(): void { this.fetch(this.offset); }

  private fetch(offset: number): void {
    if (this.loading()) return;
    this.loading.set(true);
    this.api.listSessions(undefined, this.pageSize, offset).subscribe({
      next: page => {
        this.rows.update(rs => {
          if (offset === 0) return page;
          // drop stragglers already seen — ordering may shift under a live ingest
          const seen = new Set(rs.map(r => r.session_id));
          return [...rs, ...page.filter(r => !seen.has(r.session_id))];
        });
        this.offset = offset + page.length;
        this.hasMore.set(page.length === this.pageSize);
        this.loading.set(false); this.loaded.set(true);
      },
      error: (e: HttpErrorResponse) => {
        this.error.set(`sessions unavailable (${e.status || e.message})` +
          ` — is forge-lens serve running?`);
        this.loading.set(false); this.loaded.set(true);
      },
    });
  }
  short(id: string): string { return id.length > 16 ? `${id.slice(0, 12)}…` : id; }
  copyId(id: string): void { void navigator.clipboard.writeText(id); }
}
