import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { JsonPipe, KeyValuePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Api, EvalResult, EvalSpec, ReplayTimeline, Span, Trace, TraceEval }
  from './api';

interface Row { span: Span; depth: number; }

/** One input in the per-evaluator params form — `kind` drives parsing
 *  in `evalParamsFor`: number → finite Number, int → integer ≥ 0,
 *  bool → true/false, list → comma-split string[], json → JSON.parse
 *  with a raw-string fallback, text → as typed. `required` is a UI
 *  hint (`*`) — an empty field is omitted from the spec, never
 *  invented; the backend reports the miss as `evaluator_error`. */
interface EvalParam {
  key: string;
  kind: 'number' | 'int' | 'text' | 'bool' | 'list' | 'json';
  required?: boolean;
  hint?: string;
}

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
        @for (n of checkedParamEvals(); track n) {
          <div class="eval-params" style="display:flex; gap:.5rem;
               flex-wrap:wrap; align-items:center; margin-top:.4rem">
            <code>{{ n }}</code>
            @for (p of paramsFor(n); track p.key) {
              <label style="display:flex; gap:.3rem; align-items:center">
                {{ p.key }}{{ p.required ? '*' : '' }}
                @if (p.kind === 'bool') {
                  <select [value]="evalParam(n, p.key)"
                          (change)="setEvalParam(n, p.key, $any($event.target).value)">
                    <option value="">default</option>
                    <option value="true">true</option>
                    <option value="false">false</option>
                  </select>
                } @else {
                  <input style="width:9rem" [value]="evalParam(n, p.key)"
                         [placeholder]="p.hint ?? p.kind"
                         (input)="setEvalParam(n, p.key, $any($event.target).value)">
                }
              </label>
            }
          </div>
        }
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
      <details class="panel replay">
        <summary>replay{{
          replay() ? ' — ' + replay()!.entries.length + ' events · ' +
            replay()!.total_ms + 'ms' : '' }}</summary>
        @if (replayErr()) { <p class="err-banner">{{ replayErr() }}</p> }
        @else if (!replayLoaded()) { <p class="muted">loading…</p> }
        @else if (replay(); as rp) {
          @if (rp.entries.length === 0) {
            <p class="muted">no recorded timeline — the trace has no spans.</p>
          } @else {
            <p class="muted">{{ rp.span_count }} spans ·
              {{ rp.total_ms }}ms total
              @if (rp.unclosed_spans > 0) {
                · {{ rp.unclosed_spans }} unclosed }</p>
            <table class="replay-table">
              <thead><tr>
                <th>#</th><th>+ms</th><th>t</th><th>kind</th>
                <th>name</th><th>event</th><th>status</th><th>span_id</th>
              </tr></thead>
              <tbody>
                @for (e of replayEntries(); track $index) {
                  <tr [class.err]="e.status === 'error'">
                    <td>{{ $index + 1 }}</td>
                    <td>{{ e.offset_ms }}</td>
                    <td>{{ e.t }}</td>
                    <td>{{ e.kind }}</td>
                    <td>{{ e.name }}</td>
                    <td>{{ e.event ?? '' }}</td>
                    <td>{{ e.status ?? '' }}</td>
                    <td><code>{{ e.span_id }}</code></td>
                  </tr>
                }
              </tbody>
            </table>
            @if (rp.entries.length > replayShown()) {
              <button (click)="showMoreReplay()">show more
                ({{ rp.entries.length - replayShown() }} hidden)</button> }
          }
        }
      </details>
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
  /** Mirrors forge_lens.eval.evaluators.EVALUATORS — checking a
   *  param-taking evaluator reveals its form row below. */
  readonly evaluatorNames = [
    'error_free', 'latency_budget', 'token_budget', 'tool_success',
    'has_span', 'exact_match', 'token_f1', 'allowed_tools',
    'task_completion', 'contains', 'regex', 'tool_call_count',
  ];
  /** Param form per evaluator, mirroring the keys each reads in
   *  forge_lens.eval.evaluators (canonical names only — e.g.
   *  `max_duration_ms`, not the `budget_ms` alias). Absent names take
   *  no params (error_free, tool_success). */
  readonly evaluatorParams: Record<string, EvalParam[]> = {
    latency_budget: [
      { key: 'max_duration_ms', kind: 'number', required: true, hint: 'ms > 0' },
    ],
    token_budget: [
      { key: 'max_total_tokens', kind: 'number', required: true, hint: 'tokens' },
    ],
    has_span: [
      { key: 'name', kind: 'text', hint: 'span name' },
      { key: 'kind', kind: 'text', hint: 'llm|tool|agent|…' },
    ],
    exact_match: [
      { key: 'field', kind: 'text', required: true, hint: 'attribute' },
      { key: 'expected', kind: 'json', hint: 'JSON value' },
    ],
    token_f1: [
      { key: 'field', kind: 'text', required: true, hint: 'attribute' },
      { key: 'expected', kind: 'text', required: true, hint: 'expected text' },
    ],
    allowed_tools: [
      { key: 'allowed', kind: 'list', required: true, hint: 'a, b, c' },
    ],
    task_completion: [
      { key: 'require_no_error', kind: 'bool' },
    ],
    contains: [
      { key: 'text', kind: 'text', required: true, hint: 'substring' },
      { key: 'field', kind: 'text', hint: 'default output.value' },
      { key: 'case_sensitive', kind: 'bool' },
    ],
    regex: [
      { key: 'pattern', kind: 'text', required: true, hint: 'pattern' },
      { key: 'field', kind: 'text', hint: 'default output.value' },
      { key: 'case_sensitive', kind: 'bool' },
    ],
    tool_call_count: [
      { key: 'min', kind: 'int', required: true, hint: 'int ≥ 0' },
      { key: 'max', kind: 'int', required: true, hint: 'int ≥ 0' },
      { key: 'tool', kind: 'text', hint: 'tool.name' },
    ],
  };
  evalSel = signal<Set<string>>(new Set(['error_free', 'tool_success']));
  evalRunResults = signal<EvalResult[] | null>(null);
  evalRunning = signal(false);
  evalErr = signal<string | null>(null);
  /** GET /v1/traces/{id}/replay — fetched once on init, rendered in the
   *  server's own order (no client-side re-sort). */
  replay = signal<ReplayTimeline | null>(null);
  replayErr = signal<string | null>(null);
  replayLoaded = signal(false);
  /** Cap on rendered timeline rows — mirrors the datasets table. */
  replayShown = signal(200);
  replayEntries = computed(() =>
    this.replay()?.entries.slice(0, this.replayShown()) ?? []);
  showMoreReplay(): void { this.replayShown.update(n => n + 500); }
  /** Raw param input text — evaluator → key → typed string. */
  evalParams = signal<Record<string, Record<string, string>>>({});
  /** Checked evaluators that take params — sorted for stable render. */
  checkedParamEvals = computed(() =>
    [...this.evalSel()].filter(n => n in this.evaluatorParams).sort());
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
    this.api.getReplay(this.id).subscribe({
      next: r => { this.replay.set(r); this.replayLoaded.set(true); },
      error: (e: HttpErrorResponse) => {
        this.replayErr.set(`replay unavailable (${e.status || e.message})`);
        this.replayLoaded.set(true);
      },
    });
  }

  toggleEval(name: string): void {
    this.evalSel.update(sel => {
      const next = new Set(sel);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  }
  evalParam(name: string, key: string): string {
    return this.evalParams()[name]?.[key] ?? '';
  }
  setEvalParam(name: string, key: string, value: string): void {
    this.evalParams.update(m => ({ ...m, [name]: { ...m[name], [key]: value } }));
  }
  paramsFor(name: string): EvalParam[] { return this.evaluatorParams[name] ?? []; }
  runEval(): void {
    if (this.evalRunning() || this.evalSel().size === 0) return;
    const specs: EvalSpec[] = [];
    for (const n of [...this.evalSel()].sort()) {
      const params = this.evalParamsFor(n);
      if (typeof params === 'string') { this.evalErr.set(params); return; }
      specs.push(Object.keys(params).length ? { name: n, params } : { name: n });
    }
    this.evalRunning.set(true);
    this.evalErr.set(null);
    this.api.runEval(this.id, specs).subscribe({
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

  /** Typed params for one evaluator → spec `params`, or an error string.
   *  Empty fields are omitted (a missing required param comes back as
   *  `evaluator_error` + reason, rendered `n/a`); a non-empty value
   *  that fails its kind check blocks the run before POST. */
  private evalParamsFor(name: string): Record<string, unknown> | string {
    const raw = this.evalParams()[name] ?? {};
    const out: Record<string, unknown> = {};
    for (const p of this.paramsFor(name)) {
      const v = (raw[p.key] ?? '').trim();
      if (v === '') continue;
      switch (p.kind) {
        case 'number': {
          const num = Number(v);
          if (!Number.isFinite(num)) return `${name}: ${p.key} must be a number`;
          out[p.key] = num;
          break;
        }
        case 'int': {
          const num = Number(v);
          if (!Number.isInteger(num) || num < 0) {
            return `${name}: ${p.key} must be an integer ≥ 0`;
          }
          out[p.key] = num;
          break;
        }
        case 'bool': out[p.key] = v === 'true'; break;
        case 'list':
          out[p.key] = v.split(',').map(s => s.trim()).filter(s => s !== '');
          break;
        case 'json':
          try { out[p.key] = JSON.parse(v) as unknown; } catch { out[p.key] = v; }
          break;
        default: out[p.key] = v;
      }
    }
    return out;
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
