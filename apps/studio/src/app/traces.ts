import { Component, OnInit, inject, signal } from '@angular/core';
import { RouterLink, ActivatedRoute, Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { DatePipe } from '@angular/common';
import { Api, TraceRow } from './api';

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
  </form>
  @if (rows().length === 0) { <p class="muted">no traces — instrument a run or check the API base.</p> }
  <table>
    <thead><tr>
      <th (click)="sortBy('started_at')">started</th>
      <th>trace</th><th (click)="sortBy('status')">status</th>
      <th (click)="sortBy('span_count')">spans</th>
    </tr></thead>
    <tbody>
      @for (t of sorted(); track t.trace_id) {
        <tr [class.err]="t.status === 'error'">
          <td>{{ t.started_at | date:'HH:mm:ss.SSS' }}</td>
          <td><a [routerLink]="['/traces', t.trace_id]">{{ t.trace_id }}</a></td>
          <td>{{ t.status }}</td><td>{{ t.span_count }}</td>
        </tr>
      }
    </tbody>
  </table>`,
})
export class TracesComponent implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  rows = signal<TraceRow[]>([]);
  status = '';
  limit = 100;
  sortKey = signal<'started_at' | 'status' | 'span_count'>('started_at');
  sorted = signal<TraceRow[]>([]);

  ngOnInit(): void {
    this.status = this.route.snapshot.queryParamMap.get('status') ?? '';
    this.load();
  }
  load(): void {
    // URL-state sync — deep-linkable filter state
    void this.router.navigate([], {
      queryParams: { status: this.status || null }, queryParamsHandling: 'merge',
    });
    this.api.listTraces({ status: this.status || undefined, limit: this.limit })
      .subscribe(r => { this.rows.set(r.items); this.applySort(); });
  }
  sortBy(k: 'started_at' | 'status' | 'span_count'): void {
    this.sortKey.set(k); this.applySort();
  }
  private applySort(): void {
    const k = this.sortKey();
    this.sorted.set([...this.rows()].sort((a, b) =>
      String(b[k] ?? '').localeCompare(String(a[k] ?? ''))));
  }
}
