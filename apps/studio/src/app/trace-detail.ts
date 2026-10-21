import { Component, OnInit, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { JsonPipe, KeyValuePipe, NgClass } from '@angular/common';
import { Api, Span, Trace } from './api';

interface Row { span: Span; depth: number; }

@Component({
  selector: 'lens-trace-detail',
  imports: [RouterLink, JsonPipe, KeyValuePipe, NgClass],
  template: `
  <a routerLink="/">← traces</a>
  <h1>Trace {{ id }}</h1>
  @if (trace()) {
    <p>status: <b [ngClass]="trace()!.status.code">{{ trace()!.status.code }}</b>
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
  tabs = ['waterfall', 'evidence'] as const;
  rows = signal<Row[]>([]);
  private t0 = 0; private span = 1;
  extras = signal<Record<string, unknown>>({});
  sections: [string, () => unknown][] = [
    ['cost', () => this.extras()['cost']],
    ['failure', () => this.extras()['failure']],
    ['loops', () => this.extras()['loops']],
    ['graph analysis', () => this.extras()['graph']],
  ];

  ngOnInit(): void {
    this.id = this.route.snapshot.paramMap.get('id') ?? '';
    this.api.getTrace(this.id).subscribe(t => {
      this.trace.set(t);
      const sorted = [...t.spans].sort((a, b) =>
        a.start_time.localeCompare(b.start_time));
      const starts = sorted.map(s => Date.parse(s.start_time));
      const ends = sorted.map(s => Date.parse(s.end_time ?? s.start_time));
      this.t0 = Math.min(...starts);
      this.span = Math.max(1, Math.max(...ends) - this.t0);
      const depth = new Map<string, number>();
      const byParent = new Map<string | null, Span[]>();
      for (const s of sorted) {
        const k = s.parent_span_id ?? null;
        byParent.set(k, [...(byParent.get(k) ?? []), s]);
      }
      const out: Row[] = [];
      const walk = (pid: string | null, d: number) => {
        for (const s of byParent.get(pid) ?? []) {
          out.push({ span: s, depth: d });
          walk(s.span_id, d + 1);
        }
      };
      walk(null, 0);
      const seen = new Set(out.map(r => r.span.span_id));
      for (const s of sorted) if (!seen.has(s.span_id)) out.push({ span: s, depth: 0 });
      this.rows.set(out);
      void depth;
    });
    this.api.getCost(this.id).subscribe(c => this.extras.update(e => ({ ...e, cost: c })));
    this.api.getFailure(this.id).subscribe(f => this.extras.update(e => ({ ...e, failure: f })));
    this.api.getLoops(this.id).subscribe(l => this.extras.update(e => ({ ...e, loops: l })));
    this.api.getGraphAnalysis(this.id).subscribe(g => this.extras.update(e => ({ ...e, graph: g })));
  }
  pct(iso: string): number { return (Date.parse(iso) - this.t0) / this.span * 100; }
  widthPct(s: Span): number {
    const d = Date.parse(s.end_time ?? s.start_time) - Date.parse(s.start_time);
    return Math.max(0.5, d / this.span * 100);
  }
}
