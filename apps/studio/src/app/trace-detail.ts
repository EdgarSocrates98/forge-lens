import {
  Component, ElementRef, OnDestroy, OnInit, computed, inject, signal,
} from '@angular/core';
import { ActivatedRoute, RouterLink } from '@angular/router';
import { JsonPipe, KeyValuePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import {
  AnalysisRun, Api, CorrelateReport, EfficiencyReport, EvalResult,
  EvalSpec, EvalSuggestion, IncidentBundle, Recommendation,
  RecommendationReport, ReplayEntry, ReplayTimeline, SecurityReport,
  Span, Trace, TraceEval,
} from './api';

/** `left`/`width` are the waterfall bar percentages — precomputed in
 *  `buildRows` (one Date.parse per span) so change detection never
 *  re-parses ISO strings per row. */
interface Row { span: Span; depth: number; left: number; width: number; }

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
  styles: [`
    .replay-controls .ctl-row { display: flex; gap: .4rem;
      align-items: center; flex-wrap: wrap; margin-bottom: .4rem; }
    .replay-controls:focus { outline: 1px solid var(--accent);
      outline-offset: 2px; border-radius: 4px; }
    .replay-controls .pos { font-weight: 600; min-width: 4rem;
      text-align: center; }
    .replay-table tr.cur td {
      background: color-mix(in srgb, var(--accent) 15%, transparent); }
  `],
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
            <span class="bar" [style.margin-left.%]="r.left"
                  [style.width.%]="r.width"
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
      <details class="panel incident" open>
        <summary>incident{{ incidentSummary() }}</summary>
        @switch (extraState('incident')) {
          @case ('loading') { <p class="muted">loading…</p> }
          @case ('unavailable') { <p class="muted">incident bundle unavailable</p> }
          @default {
            @if (incident(); as inc) {
              <p class="muted">
                status {{ inc.status }} ·
                category <b>{{ inc.failure['category'] ?? '—' }}</b>
                @if (inc.failure['detail']; as fd) { · {{ fd }} }
                @if (inc.bundle_version) { · bundle v{{ inc.bundle_version }} }
              </p>
              @if (inc.hypotheses.length) {
                <h4>hypotheses</h4>
                @for (h of inc.hypotheses; track h.hypothesis_id) {
                  <details class="hyp">
                    <summary><b>{{ h.label }}</b> — {{ h.status }}
                      ({{ h.confidence }})</summary>
                    <p class="muted">{{ h.symptom }}</p>
                    @if (h.supporting_evidence.length) {
                      <div class="ev"><b>supporting</b><ul>
                        @for (e of h.supporting_evidence; track e) {
                          <li>{{ e }}</li> }
                      </ul></div> }
                    @if (h.contradictory_evidence.length) {
                      <div class="ev"><b>contradictory</b><ul>
                        @for (e of h.contradictory_evidence; track e) {
                          <li>{{ e }}</li> }
                      </ul></div> }
                    @if (h.missing_evidence.length) {
                      <div class="ev"><b>missing evidence</b><ul>
                        @for (e of h.missing_evidence; track e) {
                          <li>{{ e }}</li> }
                      </ul></div> }
                    @if (h.suggested_verification.length) {
                      <div class="ev"><b>verify by</b><ul>
                        @for (e of h.suggested_verification; track e) {
                          <li>{{ e }}</li> }
                      </ul></div> }
                  </details>
                }
              }
              @if (inc.eval_suggestions.length) {
                <h4>eval suggestions
                  <span class="muted">candidate — review &amp; apply by hand</span></h4>
                <table class="eval-sugg">
                  <thead><tr>
                    <th>kind</th><th>name</th><th>params</th>
                    <th>fill in</th><th>rationale</th><th>evidence</th>
                  </tr></thead>
                  <tbody>
                    @for (s of inc.eval_suggestions; track $index) {
                      <tr>
                        <td>{{ s.kind }}</td>
                        <td><code>{{ s.name }}</code></td>
                        <td><code>{{ s.params | json }}</code></td>
                        <td>{{ s.params_required.join(', ') || '—' }}</td>
                        <td>{{ s.rationale }}</td>
                        <td>{{ suggEvidence(s) }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
              }
              @if (inc.waste.length) {
                <h4>waste</h4>
                <table class="waste">
                  <thead><tr>
                    <th>kind</th><th>spans</th><th>est usd</th><th>detail</th>
                  </tr></thead>
                  <tbody>
                    @for (w of inc.waste; track $index) {
                      <tr>
                        <td>{{ w.kind }}</td>
                        <td><code>{{ w.span_ids.join(', ') }}</code></td>
                        <td>{{ w.estimated_usd === null ? '—' : w.estimated_usd }}</td>
                        <td>{{ w.detail }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
              }
              @if (!inc.hypotheses.length && !inc.eval_suggestions.length
                   && !inc.waste.length) {
                <p class="muted">no hypotheses, eval suggestions or waste —
                  nothing flagged beyond the classification above.</p>
              }
            }
          }
        }
      </details>
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
            <!-- tabindex+keydown: ←/→ step, space play/pause — scoped
                 to this panel's focus, never global -->
            <div class="replay-controls" tabindex="0"
                 (keydown)="replayKey($event)"
                 aria-label="replay controls — arrow keys step, space plays/pauses">
              <div class="ctl-row">
                <button (click)="replayStart()" [disabled]="replayPos() === 0"
                        aria-label="jump to start" title="start">|&#171;</button>
                <button (click)="stepReplay(-1)" [disabled]="replayPos() === 0"
                        aria-label="step back" title="step back">&#8249;</button>
                <button (click)="toggleReplay()">
                  {{ playing() ? 'pause' : 'play' }}</button>
                <button (click)="stepReplay(1)"
                        [disabled]="replayPos() >= rp.entries.length - 1"
                        aria-label="step forward" title="step">&#8250;</button>
                <button (click)="replayEnd()"
                        [disabled]="replayPos() >= rp.entries.length - 1"
                        aria-label="jump to end" title="end">&#187;|</button>
                <span class="pos">{{ replayPos() + 1 }}/{{ rp.entries.length }}</span>
                <label>speed
                  <select [value]="speed()" aria-label="playback speed"
                          (change)="setSpeed($any($event.target).value)">
                    @for (s of speeds; track s) {
                      <option [value]="s">{{ s }}&#215;</option> }
                  </select>
                </label>
                <button (click)="jumpTo('error')" [disabled]="!hasErrorEntry()">
                  next error</button>
                <button (click)="jumpTo('tool')" [disabled]="!hasToolEntry()">
                  next tool</button>
                <button (click)="jumpTo('event')" [disabled]="!hasEventEntry()">
                  next event</button>
                <span class="muted">&#8592;/&#8594; step · space play/pause</span>
              </div>
            </div>
            <table class="replay-table">
              <thead><tr>
                <th>#</th><th>+ms</th><th>t</th><th>kind</th>
                <th>name</th><th>event</th><th>status</th><th>span_id</th>
              </tr></thead>
              <tbody>
                @for (e of replayEntries(); track $index) {
                  <tr [class.err]="e.status === 'error'"
                      [class.cur]="$index === replayPos()">
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
      <details class="panel intel-security">
        <summary>security{{ securitySummary() }}</summary>
        @switch (extraState('security')) {
          @case ('loading') { <p class="muted">loading…</p> }
          @case ('unavailable') { <p class="muted">security report unavailable</p> }
          @default {
            @if (security(); as r) {
              @if (r.observations.length === 0) {
                <p class="muted">no observations — {{ r.mode }} mode</p>
              } @else {
                <table>
                  <thead><tr>
                    <th>severity</th><th>kind</th><th>span</th><th>detail</th>
                  </tr></thead>
                  <tbody>
                    @for (o of r.observations; track $index) {
                      <tr [class.err]="o.severity === 'high'">
                        <td>{{ o.severity }}</td>
                        <td>{{ o.kind }}</td>
                        <td><code>{{ o.span_id }}</code></td>
                        <td>{{ o.detail }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
                <p class="muted">{{ r.mode }} mode — reports only, nothing blocked</p>
              }
            }
          }
        }
      </details>
      <details class="panel intel-efficiency">
        <summary>efficiency{{ efficiencySummary() }}</summary>
        @switch (extraState('efficiency')) {
          @case ('loading') { <p class="muted">loading…</p> }
          @case ('unavailable') { <p class="muted">efficiency report unavailable</p> }
          @default {
            @if (efficiency(); as e) {
              <dl>
                <dt>outcome</dt>
                <dd>{{ e.outcome }} ({{ e.succeeded ? 'succeeded' : 'not ok' }})</dd>
                <dt>tool calls</dt><dd>{{ e.tool_calls }}</dd>
                <dt>llm calls</dt><dd>{{ e.llm_calls }}</dd>
                <dt>tokens</dt>
                <dd>{{ e.input_tokens }} in · {{ e.output_tokens }} out</dd>
                <dt>usage coverage</dt><dd>{{ e.usage_coverage }}</dd>
                <dt>error spans</dt><dd>{{ e.error_spans }}</dd>
                <dt>retries observed</dt><dd>{{ e.retries_observed }}</dd>
              </dl>
            }
          }
        }
      </details>
      <details class="panel intel-recs">
        <summary>recommendations{{ recsSummary() }}</summary>
        @switch (extraState('recommendations')) {
          @case ('loading') { <p class="muted">loading…</p> }
          @case ('unavailable') { <p class="muted">recommendations unavailable</p> }
          @default {
            @if (recommendations(); as r) {
              @if (r.recommendations.length === 0) {
                <p class="muted">{{ r.note }}</p>
              } @else {
                <table>
                  <thead><tr>
                    <th>kind</th><th>evidence</th><th>detail</th>
                  </tr></thead>
                  <tbody>
                    @for (rec of r.recommendations; track $index) {
                      <tr>
                        <td>{{ rec.kind }}</td>
                        <td>{{ recEvidence(rec) }}</td>
                        <td>{{ rec.detail }}</td>
                      </tr>
                    }
                  </tbody>
                </table>
                <p class="muted">{{ r.note }}</p>
              }
            }
          }
        }
      </details>
      <details class="panel intel-correlate">
        <summary>correlate{{ corrSummary() }}</summary>
        @switch (extraState('correlate')) {
          @case ('loading') { <p class="muted">loading…</p> }
          @case ('unavailable') { <p class="muted">correlation report unavailable</p> }
          @default {
            @if (correlate(); as r) {
              @if (r.correlations.length === 0) {
                <p class="muted">{{ r.note }}</p>
              } @else {
                <table>
                  <thead><tr>
                    <th>kind</th><th>detail</th><th>evidence spans</th>
                  </tr></thead>
                  <tbody>
                    @for (c of r.correlations; track $index) {
                      <tr>
                        <td>{{ c.kind }}</td>
                        <td>{{ c.detail }}<br>
                          <span class="muted">{{ c.caveat }}</span></td>
                        <td><code>{{ c.evidence_span_ids.join(', ') }}</code></td>
                      </tr>
                    }
                  </tbody>
                </table>
                <p class="muted">{{ r.note }}</p>
              }
            }
          }
        }
      </details>
      <details class="panel analyze">
        <summary>analyze — run a specialist</summary>
        <div class="filters" style="display:flex; gap:.5rem;
             flex-wrap:wrap; align-items:center">
          <input list="lens-specialists" size="30" [value]="specialistSel"
                 (input)="specialistSel = $any($event.target).value"
                 placeholder="specialist name" aria-label="specialist name" />
          <datalist id="lens-specialists">
            @for (s of specialists() ?? []; track s) {
              <option [value]="s"></option> }
          </datalist>
          <button (click)="runAnalysis()"
                  [disabled]="analysisRunning() || !specialistSel.trim()">
            {{ analysisRunning() ? 'running…' : 'run' }}</button>
          <span class="muted">POST /v1/analyze/&lt;specialist&gt;/&lt;trace&gt;
            — default budgets</span>
        </div>
        @if (analysisErr()) { <p class="err-banner">{{ analysisErr() }}</p> }
        @if (analysisRun(); as ar) {
          <p class="muted">{{ ar.stop_reason }} ·
            {{ ar.iterations }} iteration(s) ·
            {{ ar.tokens_observed }} tokens observed{{
              ar.cancelled ? ' · cancelled' : '' }}</p>
          @for (fd of ar.findings; track $index) {
            <pre>{{ fd | json }}</pre> }
          @if (!ar.findings.length) { <p class="muted">no findings</p> }
        }
      </details>
    }
  } @else { <p class="muted">loading…</p> }`,
})
export class TraceDetailComponent implements OnInit, OnDestroy {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private host = inject(ElementRef);
  id = '';
  trace = signal<Trace | null>(null);
  sel = signal<Span | null>(null);
  tab = signal<'waterfall' | 'evidence'>('waterfall');
  readonly tabs = ['waterfall', 'evidence'] as const;
  rows = signal<Row[]>([]);
  error = signal<string | null>(null);
  extras = signal<Record<string, unknown>>({});
  readonly sections: [string, () => unknown][] = [
    ['cost', () => this.extras()['cost']],
    ['failure', () => this.extras()['failure']],
    ['loops', () => this.extras()['loops']],
    ['graph analysis', () => this.extras()['graph']],
  ];

  // -- intelligence pulls (incident bundle + per-endpoint reports) ----
  /** null until the pull resolves and whenever it failed — use
   *  `extraState` to tell "still loading" from "unavailable". */
  incident = computed<IncidentBundle | null>(() =>
    this.extraAs<IncidentBundle>('incident'));
  security = computed<SecurityReport | null>(() =>
    this.extraAs<SecurityReport>('security'));
  efficiency = computed<EfficiencyReport | null>(() =>
    this.extraAs<EfficiencyReport>('efficiency'));
  recommendations = computed<RecommendationReport | null>(() =>
    this.extraAs<RecommendationReport>('recommendations'));
  correlate = computed<CorrelateReport | null>(() =>
    this.extraAs<CorrelateReport>('correlate'));

  // -- analyze (specialist runner, POST /v1/analyze/{spec}/{trace}) ---
  specialists = signal<string[] | null>(null);
  specialistSel = '';
  analysisRun = signal<AnalysisRun | null>(null);
  analysisRunning = signal(false);
  analysisErr = signal<string | null>(null);

  /** extras[key] → typed payload, or null on `{error}` placeholders. */
  private extraAs<T>(key: string): T | null {
    const v = this.extras()[key];
    if (!v || (v as { error?: string }).error !== undefined) return null;
    return v as T;
  }
  extraState(key: string): 'loading' | 'unavailable' | 'ready' {
    const v = this.extras()[key];
    if (v === undefined) return 'loading';
    return (v as { error?: string }).error !== undefined
      ? 'unavailable' : 'ready';
  }
  incidentSummary = computed(() => {
    const i = this.incident();
    if (!i) return this.extraState('incident') === 'unavailable'
      ? ' — unavailable' : '';
    const parts: string[] = [];
    if (i.hypotheses?.length) parts.push(`${i.hypotheses.length} hypotheses`);
    if (i.eval_suggestions?.length) {
      parts.push(`${i.eval_suggestions.length} eval suggestion(s)`);
    }
    if (i.waste?.length) parts.push(`${i.waste.length} waste`);
    return ' — ' + (parts.length ? parts.join(' · ') : 'clean');
  });
  securitySummary = computed(() => {
    const r = this.security();
    return r ? ` — ${r.observations.length} observation(s)` : '';
  });
  efficiencySummary = computed(() => {
    const e = this.efficiency();
    return e ? ` — ${e.llm_calls} llm · ${e.tool_calls} tool` : '';
  });
  recsSummary = computed(() => {
    const r = this.recommendations();
    return r ? ` — ${r.recommendations.length} candidate` : '';
  });
  corrSummary = computed(() => {
    const r = this.correlate();
    return r ? ` — ${r.correlations.length} association(s)` : '';
  });
  /** Evidence column for an eval_suggestion row: `category · n trace(s)`
   *  plus ' recurring' when the server flagged ≥2 distinct traces. */
  suggEvidence(s: EvalSuggestion): string {
    const cat = s.evidence?.['category'] ?? s.evidence?.['categories'];
    const count = s.evidence?.['count'];
    const base = `${cat ?? '—'} · ${count ?? '?'} trace(s)`;
    return s.evidence?.['recurring'] === true ? base + ' · recurring' : base;
  }
  /** Evidence column for a recommendation — tool name, spans, or the
   *  counters the specific rule fired on. */
  recEvidence(r: Recommendation): string {
    if (r.tool) return r.failures ? `${r.tool} ×${r.failures}` : r.tool;
    if (r.spans?.length) return r.spans.join(', ');
    if (r.tool_calls) return `${r.tool_calls} calls`;
    return '—';
  }

  runAnalysis(): void {
    const sp = this.specialistSel.trim();
    if (!sp || this.analysisRunning()) return;
    this.analysisRunning.set(true);
    this.analysisErr.set(null);
    this.analysisRun.set(null);
    this.api.runAnalysis(this.id, sp).subscribe({
      next: r => { this.analysisRunning.set(false); this.analysisRun.set(r); },
      error: (e: HttpErrorResponse) => {
        this.analysisRunning.set(false);
        this.analysisErr.set(`analysis failed (${e.status || e.message})`);
      },
    });
  }

  /** Mirrors forge_lens.eval.evaluators.EVALUATORS — checking a
   *  param-taking evaluator reveals its form row below. */
  readonly evaluatorNames = [
    'allowed_tools', 'claims_grounded', 'contains', 'error_free',
    'evidence_coverage', 'exact_match', 'has_span', 'human_review',
    'latency_budget', 'precision_recall', 'regex', 'schema_validation',
    'task_completion', 'token_budget', 'token_f1', 'tool_call_count',
    'tool_success',
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
    schema_validation: [
      { key: 'schema', kind: 'json', required: true, hint: 'JSON Schema subset' },
      { key: 'field', kind: 'text', hint: 'default output.value' },
    ],
    evidence_coverage: [
      { key: 'required_attrs', kind: 'list', required: true, hint: 'attr keys' },
      { key: 'min_coverage', kind: 'number', hint: '(0,1] default 1.0' },
    ],
    human_review: [
      { key: 'rubric', kind: 'text', hint: 'optional queue marker' },
    ],
    precision_recall: [
      { key: 'expected_items', kind: 'json', required: true, hint: 'JSON list' },
      { key: 'field', kind: 'text', hint: 'XOR with tool' },
      { key: 'tool', kind: 'bool', hint: 'XOR with field' },
    ],
    claims_grounded: [
      { key: 'required_source_attr', kind: 'text', required: true, hint: 'attr key' },
      { key: 'min_overlap', kind: 'number', hint: '(0,1] default 0.5' },
      { key: 'field', kind: 'text', hint: 'default output.value' },
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
  /** Cap on rendered timeline rows — mirrors the datasets table.
   *  Stepping past it expands it (`setPos`), so playback can always
   *  reach entries the cap hasn't revealed yet. */
  replayShown = signal(200);
  replayEntries = computed(() =>
    this.replay()?.entries.slice(0, this.replayShown()) ?? []);
  showMoreReplay(): void { this.replayShown.update(n => n + 500); }

  // -- playback controls (operate on the full loaded entries, no SSE)
  /** Index into `replay().entries` the controls are parked on. */
  replayPos = signal(0);
  playing = signal(false);
  /** Multiplier options; 1× = 1000ms per step, so the delay is
   *  `1000 / speed`. */
  speed = signal(1);
  readonly speeds = [0.5, 1, 2, 4];
  private timer: ReturnType<typeof setInterval> | null = null;
  /** Span ids whose span `kind` is 'tool' — observed trace evidence,
   *  the honest source for "next tool" (replay entries don't carry
   *  span kind). */
  toolSpanIds = computed(() => new Set(
    (this.trace()?.spans ?? [])
      .filter(s => s.kind === 'tool')
      .map(s => s.span_id)));
  hasErrorEntry = computed(() =>
    (this.replay()?.entries ?? []).some(e => e.status === 'error'));
  hasToolEntry = computed(() => {
    const t = this.toolSpanIds();
    return (this.replay()?.entries ?? []).some(e => t.has(e.span_id));
  });
  hasEventEntry = computed(() =>
    (this.replay()?.entries ?? []).some(e => e.kind === 'event'));

  /** Clamp-park on index `i`; expands the render cap when the target
   *  row isn't revealed yet, then scrolls it into view. */
  private setPos(i: number): void {
    const n = this.replay()?.entries.length ?? 0;
    if (n === 0) return;
    const pos = Math.min(Math.max(i, 0), n - 1);
    this.replayPos.set(pos);
    if (pos + 1 > this.replayShown()) this.replayShown.set(pos + 1);
    // defer past the DOM update the signal change schedules
    setTimeout(() => {
      const rows = this.host.nativeElement
        .querySelectorAll('.replay-table tbody tr');
      const el = rows.item(Math.min(pos, rows.length - 1));
      (el as HTMLElement | null)?.scrollIntoView?.({ block: 'nearest' });
    });
  }
  /** Manual nav always pauses playback. */
  stepReplay(d: number): void {
    this.pauseReplay();
    this.setPos(this.replayPos() + d);
  }
  replayStart(): void { this.pauseReplay(); this.setPos(0); }
  replayEnd(): void {
    this.pauseReplay();
    this.setPos((this.replay()?.entries.length ?? 1) - 1);
  }
  /** Jump forward to the next matching entry, wrapping around; the
   *  buttons are disabled when the timeline has no such entry. */
  jumpTo(kind: 'error' | 'tool' | 'event'): void {
    const es = this.replay()?.entries ?? [];
    const n = es.length;
    if (!n) return;
    const tools = this.toolSpanIds();
    const pred = (e: ReplayEntry): boolean =>
      kind === 'error' ? e.status === 'error'
        : kind === 'event' ? e.kind === 'event'
          : tools.has(e.span_id);
    for (let off = 1; off <= n; off++) {
      const i = (this.replayPos() + off) % n;
      if (pred(es[i])) { this.pauseReplay(); this.setPos(i); return; }
    }
  }
  toggleReplay(): void {
    if (this.playing()) this.pauseReplay(); else this.playReplay();
  }
  playReplay(): void {
    const n = this.replay()?.entries.length ?? 0;
    if (this.playing() || n === 0) return;
    if (this.replayPos() >= n - 1) this.setPos(0); // replay from start
    this.playing.set(true);
    this.restartTimer();
  }
  pauseReplay(): void {
    this.playing.set(false);
    if (this.timer !== null) { clearInterval(this.timer); this.timer = null; }
  }
  private restartTimer(): void {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = setInterval(() => this.tickReplay(), 1000 / this.speed());
  }
  private tickReplay(): void {
    const n = this.replay()?.entries.length ?? 0;
    if (this.replayPos() + 1 >= n) { this.pauseReplay(); return; }
    this.setPos(this.replayPos() + 1);
  }
  setSpeed(v: unknown): void {
    const s = Number(v);
    if (!Number.isFinite(s) || s <= 0) return;
    this.speed.set(s);
    if (this.playing()) this.restartTimer();
  }
  /** Scoped to the panel — the handler lives on the focusable
   *  controls container, so keys only act while it has focus.
   *  Interactive children keep their native keys (selects use arrows,
   *  buttons use space) — only arrows are repurposed there. */
  replayKey(ev: KeyboardEvent): void {
    const tag = (ev.target as HTMLElement | null)?.tagName;
    if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') return;
    if (ev.key === 'ArrowLeft') {
      ev.preventDefault(); this.stepReplay(-1);
    } else if (ev.key === 'ArrowRight') {
      ev.preventDefault(); this.stepReplay(1);
    } else if (ev.key === ' ') {
      if (tag === 'BUTTON' || tag === 'A') return; // native activation
      ev.preventDefault(); this.toggleReplay();
    }
  }
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
    pull('incident', this.api.getIncident(this.id));
    pull('security', this.api.getSecurityReport(this.id));
    pull('efficiency', this.api.getEfficiency(this.id));
    pull('recommendations', this.api.getRecommendations(this.id));
    pull('correlate', this.api.getCorrelate(this.id));
    // datalist suggestions for the specialist runner — a failure only
    // means an empty dropdown, the name field stays free-text
    this.api.listSpecialists().subscribe({
      next: s => this.specialists.set(s),
      error: () => this.specialists.set([]),
    });
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

  ngOnDestroy(): void { this.pauseReplay(); }

  private buildRows(t: Trace): void {
    this.trace.set(t);
    const sorted = [...t.spans].sort((a, b) =>
      a.start_time.localeCompare(b.start_time));
    // iterative walk — no recursion overflow on deep trees
    let min = Infinity, max = -Infinity;
    // one Date.parse per span boundary, reused for the bar geometry
    const times = new Map<string, { st: number; en: number }>();
    const byParent = new Map<string | null, Span[]>();
    for (const s of sorted) {
      const st = Date.parse(s.start_time);
      const en = Date.parse(s.end_time ?? s.start_time);
      times.set(s.span_id, { st, en });
      if (st < min) min = st;
      if (en > max) max = en;
      const k = s.parent_span_id ?? null;
      const arr = byParent.get(k); if (arr) arr.push(s); else byParent.set(k, [s]);
    }
    const t0 = min === Infinity ? 0 : min;
    const spanMs = Math.max(1, max - t0);
    const row = (s: Span, d: number): Row => {
      const ts = times.get(s.span_id)!;
      return {
        span: s, depth: d,
        left: (ts.st - t0) / spanMs * 100,
        width: Math.max(0.5, (ts.en - ts.st) / spanMs * 100),
      };
    };
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
      out.push(row(s, d));
      const kids = byParent.get(s.span_id) ?? [];
      for (let i = kids.length - 1; i >= 0; i--) stack.push({ s: kids[i], d: d + 1 });
    }
    for (const s of sorted) {
      if (!seen.has(s.span_id)) out.push(row(s, 0));
    }
    this.rows.set(out);
  }
}
