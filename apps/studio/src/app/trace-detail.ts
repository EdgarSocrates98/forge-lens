import { Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { JsonPipe, KeyValuePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, Span, Trace } from './api';

interface Row { span: Span; depth: number; }

@Component({
  selector: 'lens-trace-detail',
  imports: [RouterLink, JsonPipe, KeyValuePipe],
  template: `
  <a routerLink="/">← traces</a>
  <h1>Trace {{ id }}</h1>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (trace()) {
    <p>status: <b [class]="trace()!.status.code">{{ trace()!.status.code }}</b>
       · {{ trace()!.spans.length }} spans</p>
    <nav class="tabs">
      @for (t of tabs; track t) {
        <button [class.on]="tab() === t" (click)="tab.set(t)">{{ t }}</button>
      }
    </nav>
    @if (tab() === 'waterfall') {
      <div class="wf">
        @for (r of rows(); track r.span.span_id) {
          <button class="row" (click)="sel.set(r.span)">
            <span class="name" [style.padding-left.px]="r.depth * 14">{{ r.span.name }}</span>
            <span class="bar" [style.margin-left.%]="pct(r.span.start_time)"
                  [style.width.%]="widthPct(r.span)"
                  [class.err]="r.span.status.code === 'error'"></span>
          </button>
        }
      </div>
      @if (sel(); as s) {
        <section class="panel">
          <h3>{{ s.name }} <code>{{ s.span_id }}</code></h3>
          <p>kind {{ s.kind }} · {{ s.start_time }} → {{ s.end_time }}
             · status {{ s.status.code }}</p>
          <dl>@for (kv of s.attributes | keyvalue; track kv.key) {
            <dt>{{ kv.key }}</dt><dd>{{ kv.value | json }}</dd>
          }</dl>
        </section>
      }
    }
    @if (tab() === 'evidence') {
      @for (sec of sections; track sec[0]) {
        <h3>{{ sec[0] }}</h3><pre>{{ sec[1]() | json }}</pre>
      }
    }
  } @else { <p class="muted">loading…</p> }`,
})
export class TraceDetailComponent implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  id = '';
  trace = signal<Trace | null>(null);
  sel = signal<Span | null>(null);
  tab = signal<'waterfall' | 'evidence'>('waterfall');
  readonly tabs = ['waterfall', 'evidence'] as const;
  rows = signal<Row[]>([]);
  error = signal<string | null>(null);
  private t0 = 0; private spanMs = 1;
  extras = signal<Record<string, unknown>>({});
  readonly sections: [string, () => unknown][] = [
    ['cost', () => this.extras()['cost']],
    ['failure', () => this.extras()['failure']],
    ['loops', () => this.extras()['loops']],
    ['graph analysis', () => this.extras()['graph']],
  ];

  ngOnInit(): void {
    this.id = this.route.snapshot.paramMap.get('id') ?? '';
    this.api.getTrace(this.id).subscribe({
      next: t => this.buildRows(t),
      error: (e: HttpErrorResponse) =>
        this.error.set(`cannot load trace (${e.status || e.message})`),
    });
    const pull = (key: string, obs: import('rxjs').Observable<unknown>) =>
      obs.subscribe({
        next: v => this.extras.update(e => ({ ...e, [key]: v })),
        error: () => this.extras.update(e => ({ ...e, [key]: { error: 'unavailable' } })),
      });
    pull('cost', this.api.getCost(this.id));
    pull('failure', this.api.getFailure(this.id));
    pull('loops', this.api.getLoops(this.id));
    pull('graph', this.api.getGraphAnalysis(this.id));
  }

  private buildRows(t: Trace): void {
    this.trace.set(t);
    const sorted = [...t.spans].sort((a, b) =>
      a.start_time.localeCompare(b.start_time));
    // iterative walk — no recursion overflow on deep trees
    let min = Infinity, max = -Infinity;
    const byParent = new Map<string | null, Span[]>();
    for (const s of sorted) {
      const st = Date.parse(s.start_time);
      const en = Date.parse(s.end_time ?? s.start_time);
      if (st < min) min = st;
      if (en > max) max = en;
      const k = s.parent_span_id ?? null;
      const arr = byParent.get(k); if (arr) arr.push(s); else byParent.set(k, [s]);
    }
    this.t0 = min === Infinity ? 0 : min;
    this.spanMs = Math.max(1, max - this.t0);
    const out: Row[] = [];
    const seen = new Set<string>();
    const stack: { s: Span; d: number }[] = [];
    for (const s of [...(byParent.get(null) ?? [])].reverse()) {
      stack.push({ s, d: 0 });
    }
    while (stack.length) {
      const { s, d } = stack.pop()!;
      if (seen.has(s.span_id)) continue;
      seen.add(s.span_id);
      out.push({ span: s, depth: d });
      const kids = byParent.get(s.span_id) ?? [];
      for (let i = kids.length - 1; i >= 0; i--) stack.push({ s: kids[i], d: d + 1 });
    }
    for (const s of sorted) if (!seen.has(s.span_id)) out.push({ span: s, depth: 0 });
    this.rows.set(out);
  }

  pct(iso: string): number { return (Date.parse(iso) - this.t0) / this.spanMs * 100; }
  widthPct(s: Span): number {
    const d = Date.parse(s.end_time ?? s.start_time) - Date.parse(s.start_time);
    return Math.max(0.5, d / this.spanMs * 100);
  }
}
