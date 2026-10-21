import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { JsonPipe, KeyValuePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, EvalResult, Span, Trace, TraceEval } from './api';

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
      <h3>eval</h3>
      <section class="panel eval">
        <div style="display:flex; gap:.6rem; flex-wrap:wrap; align-items:center">
          @for (n of evaluatorNames; track n) {
            <label><input type="checkbox" [checked]="evalSel().has(n)"
                          (change)="toggleEval(n)"> {{ n }}</label>
          }
          <button (click)="runEval()"
                  [disabled]="evalRunning() || evalSel().size === 0">
            {{ evalRunning() ? 'running…' : 'run selected' }}</button>
        </div>
        @if (evalErr()) { <p class="err-banner">{{ evalErr() }}</p> }
        @switch (evalState()) {
          @case ('loading') { <p class="muted">loading…</p> }
          @case ('unavailable') { <p class="muted">eval unavailable</p> }
          @case ('empty') { <p class="muted">no eval results</p> }
          @case ('results') {
            <table class="eval-table">
              <thead><tr>
                <th>evaluator</th><th>metric</th><th>score</th><th>reason</th>
              </tr></thead>
              <tbody>
                @for (r of evalResults(); track $index) {
                  <tr>
                    <td>{{ r.evaluator }}</td>
                    <td>{{ r.metric }}</td>
                    <td>{{ r.score === null ? 'n/a' : r.score }}</td>
                    <td>{{ evalReason(r) }}</td>
                  </tr>
                }
              </tbody>
            </table>
          }
        }
      </section>
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
  /** Mirrors forge_lens.eval.evaluators.EVALUATORS — specs run without
   *  params; param-required ones come back `score: null` + a reason. */
  readonly evaluatorNames = [
    'error_free', 'latency_budget', 'token_budget', 'tool_success',
    'has_span', 'exact_match', 'token_f1', 'allowed_tools',
    'task_completion', 'contains', 'regex', 'tool_call_count',
  ];
  evalSel = signal<Set<string>>(new Set(['error_free', 'tool_success']));
  evalRunResults = signal<EvalResult[] | null>(null);
  evalRunning = signal(false);
  evalErr = signal<string | null>(null);
  /** Table rows — a POST run overrides the auto-pulled GET battery. */
  evalResults = computed<EvalResult[]>(() => {
    const run = this.evalRunResults();
    if (run) return run;
    const v = this.extras()['eval'] as TraceEval | { error: string } | undefined;
    return v && 'results' in v ? v.results : [];
  });
  evalState = computed<'loading' | 'unavailable' | 'empty' | 'results'>(() => {
    const run = this.evalRunResults();
    if (run) return run.length ? 'results' : 'empty';
    const v = this.extras()['eval'] as TraceEval | { error: string } | undefined;
    if (v === undefined) return 'loading';
    if (!('results' in v)) return 'unavailable';
    return v.results.length ? 'results' : 'empty';
  });

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
    pull('eval', this.api.getTraceEval(this.id));
  }

  toggleEval(name: string): void {
    this.evalSel.update(sel => {
      const next = new Set(sel);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  }
  runEval(): void {
    if (this.evalRunning() || this.evalSel().size === 0) return;
    this.evalRunning.set(true);
    this.evalErr.set(null);
    this.api.runEval(this.id, [...this.evalSel()].sort()).subscribe({
      next: r => {
        this.evalRunning.set(false);
        this.evalRunResults.set(r.results[this.id] ?? []);
      },
      error: (e: HttpErrorResponse) => {
        this.evalRunning.set(false);
        this.evalErr.set(`eval run failed (${e.status || e.message})`);
      },
    });
  }
  /** `details.reason` is the evaluator's own evidence — never a verdict
   *  we invent when `score` is null. */
  evalReason(r: EvalResult): string { return r.details?.['reason'] ?? ''; }

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
