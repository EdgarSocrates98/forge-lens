import {
  Component, OnDestroy, computed, effect, inject, input, signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { JsonPipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { Subscription, forkJoin } from 'rxjs';
import {
  Api, CompareReport, EvalSpec, MetricDelta,
} from './api';

/** One score-0 eval result plus the trace it was observed on — the
 *  row shape of the failure-examples table. */
export interface EvalFailure {
  trace_id: string; evaluator: string; metric: string;
  score: number; reason: string;
}

/** Fill the fields compare_runs guarantees so the template never has
 *  to trust the wire — a truncated/error-shaped body degrades to
 *  empty cohorts instead of a crashed render. */
export function normalizeCompare(r: CompareReport): CompareReport {
  const side = (s: CompareReport['a'] | undefined) => ({
    dataset: s?.dataset ?? null,
    trace_ids: s?.trace_ids ?? [],
    n: s?.n ?? s?.trace_ids?.length ?? 0,
  });
  return {
    a: side(r?.a), b: side(r?.b),
    deltas: r?.deltas ?? [],
    ...(r?.gate
      ? { gate: { gate_passed: !!r.gate.gate_passed, failures: r.gate.failures ?? [] } }
      : {}),
    ...(r?.missing_trace_ids !== undefined
      ? { missing_trace_ids: r.missing_trace_ids } : {}),
    ...(r?.multiple_comparisons != null
      ? { multiple_comparisons: r.multiple_comparisons } : {}),
  };
}

/** Deltas whose metric carries a cost/token mean — matched on the
 *  `evaluator/metric` key the report emits (e.g. `token_budget/…`,
 *  `cost_usd/…`). Everything else with both means is a quality
 *  metric; the Pareto table only renders when both classes exist —
 *  no invented axes otherwise. */
const COSTISH = /cost|token/i;

@Component({
  selector: 'lens-compare-report',
  imports: [RouterLink, JsonPipe],
  styles: [`
    .pareto th.side { cursor: default; }
  `],
  template: `
  <p class="muted">
    a: <b>{{ rep().a.dataset ?? '(unlabeled)' }}</b> — {{ rep().a.n }} trace(s) ·
    b: <b>{{ rep().b.dataset ?? '(unlabeled)' }}</b> — {{ rep().b.n }} trace(s)
  </p>
  @if (rep().deltas.length) {
    <table>
      <thead><tr>
        <th>evaluator</th><th>metric</th><th>n a/b</th>
        <th>mean a</th><th>mean b</th><th>Δ</th>
        <th>pass a</th><th>pass b</th>
        <th>significance</th><th>p</th>
      </tr></thead>
      <tbody>
        @for (d of rep().deltas; track d.evaluator + '/' + d.metric) {
          <tr>
            <td>{{ d.evaluator }}</td><td>{{ d.metric }}</td>
            <td>{{ d.n_a }}/{{ d.n_b }}</td>
            <td>{{ num(d.mean_a) }}</td><td>{{ num(d.mean_b) }}</td>
            <td>{{ num(d.delta) }}</td>
            <td>{{ num(d.pass_rate_a) }}</td><td>{{ num(d.pass_rate_b) }}</td>
            <td>{{ d.significance }}</td>
            <td>{{ num(d.p_value) }}</td>
          </tr>
        }
      </tbody>
    </table>
  } @else {
    <p class="muted">no metric deltas — the evaluators produced no
      scored results on either cohort.</p> }
  @if (rep().multiple_comparisons) {
    <p class="muted">{{ rep().multiple_comparisons }}</p> }
  @if (rep().gate; as g) {
    <p>gate: <b [class.error]="!g.gate_passed">{{ g.gate_passed ? 'passed' : 'failed' }}</b></p>
    @for (f of g.failures; track $index) {
      <pre>{{ f | json }}</pre>
    }
  }
  @if (rep().missing_trace_ids) {
    <p class="muted">missing trace ids: {{ rep().missing_trace_ids | json }}</p> }

  @if (pareto(); as p) {
    <h4>quality vs cost/token — per-side means</h4>
    <table class="pareto">
      <thead>
        <tr>
          <th class="side"></th>
          <th [attr.colspan]="p.quality.length">quality</th>
          <th [attr.colspan]="p.cost.length">cost / token</th>
        </tr>
        <tr>
          <th class="side">side</th>
          @for (m of p.quality; track m.evaluator + '/' + m.metric) {
            <th>{{ m.evaluator }}/{{ m.metric }}</th> }
          @for (m of p.cost; track m.evaluator + '/' + m.metric) {
            <th>{{ m.evaluator }}/{{ m.metric }}</th> }
        </tr>
      </thead>
      <tbody>
        <tr>
          <td>a</td>
          @for (m of p.quality; track m.evaluator + '/' + m.metric) {
            <td>{{ num(m.mean_a) }}</td> }
          @for (m of p.cost; track m.evaluator + '/' + m.metric) {
            <td>{{ num(m.mean_a) }}</td> }
        </tr>
        <tr>
          <td>b</td>
          @for (m of p.quality; track m.evaluator + '/' + m.metric) {
            <td>{{ num(m.mean_b) }}</td> }
          @for (m of p.cost; track m.evaluator + '/' + m.metric) {
            <td>{{ num(m.mean_b) }}</td> }
        </tr>
      </tbody>
    </table>
  }

  <h4>failure examples</h4>
  @if (failNote(); as note) {
    <p class="muted">{{ note }}</p>
  } @else {
    <p class="muted">score-0 results recomputed via POST /v1/eval/run
      over both cohorts — deterministic for the evaluators used.</p>
    @if (failErr()) { <p class="err-banner">{{ failErr() }}</p> }
    @else if (failLoading()) { <p class="muted">loading…</p> }
    @else if (failures(); as fl) {
      @if (fl.length === 0) {
        <p class="muted">no failing eval results — no score-0 results
          across the cohorts.</p>
      } @else {
        <table>
          <thead><tr>
            <th>trace</th><th>evaluator</th><th>metric</th>
            <th>score</th><th>reason</th>
          </tr></thead>
          <tbody>
            @for (f of fl; track $index) {
              <tr>
                <td><a [routerLink]="['/traces', f.trace_id]">{{ f.trace_id }}</a></td>
                <td>{{ f.evaluator }}</td>
                <td>{{ f.metric }}</td>
                <td>{{ f.score }}</td>
                <td>{{ f.reason }}</td>
              </tr>
            }
          </tbody>
        </table>
      }
    }
  }`,
})
export class CompareReportComponent implements OnDestroy {
  private api = inject(Api);
  /** The compare_runs payload (from /eval/compare or an experiment
   *  run) — normalized before render so the wire can never crash it. */
  report = input.required<CompareReport>();
  /** The evaluator specs this report was produced with — needed to
   *  recompute per-trace results for the failure-examples table (the
   *  report itself only carries aggregates). */
  evaluators = input<EvalSpec[]>([]);
  rep = computed(() => normalizeCompare(this.report()));
  /** Quality-vs-cost view: both classes of metric must carry a real
   *  mean on both sides, else there is nothing honest to compare. */
  pareto = computed<{
    quality: MetricDelta[]; cost: MetricDelta[];
  } | null>(() => {
    const both = this.rep().deltas
      .filter(d => d.mean_a != null && d.mean_b != null);
    const cost = both.filter(d =>
      COSTISH.test(`${d.evaluator}/${d.metric}`));
    const quality = both.filter(d =>
      !COSTISH.test(`${d.evaluator}/${d.metric}`));
    return cost.length && quality.length ? { quality, cost } : null;
  });

  failures = signal<EvalFailure[] | null>(null);
  failErr = signal<string | null>(null);
  failLoading = signal(false);
  failNote = signal<string | null>(null);
  private failSub: Subscription | null = null;
  private fetchedKey: string | null = null;

  constructor() {
    effect(() => {
      const r = this.rep();
      const specs = this.evaluators();
      const ids = [...new Set([...r.a.trace_ids, ...r.b.trace_ids])];
      if (!ids.length || !specs.length) {
        this.fetchedKey = null;
        this.failSub?.unsubscribe();
        this.failures.set(null);
        this.failErr.set(null);
        this.failLoading.set(false);
        this.failNote.set(
          'failure examples unavailable — the report carries no ' +
          'per-trace results and no evaluator spec is known to ' +
          'recompute them with.');
        return;
      }
      this.failNote.set(null);
      // dedupe: one fetch per (ids, specs) pair — a re-render of the
      // same report never re-posts
      const key = JSON.stringify({ ids, specs });
      if (key === this.fetchedKey) return;
      this.fetchedKey = key;
      this.failLoading.set(true);
      this.failErr.set(null);
      this.failSub?.unsubscribe();
      // eval/run bounds trace_ids at 500 but compare cohorts allow
      // 500/side — chunk the union and merge the per-trace results so
      // failure examples survive full-size cohorts
      const chunks: string[][] = [];
      for (let i = 0; i < ids.length; i += 500) {
        chunks.push(ids.slice(i, i + 500));
      }
      this.failSub = forkJoin(
        chunks.map(c => this.api.runEvalSet(c, specs)),
      ).subscribe({
        next: reps => {
          this.failLoading.set(false);
          const out: EvalFailure[] = [];
          for (const rep of reps) {
            for (const [trace_id, results] of
              Object.entries(rep.results ?? {})) {
              for (const r of results ?? []) {
                if (r.score === 0) {
                  out.push({
                    trace_id,
                    evaluator: r.evaluator, metric: r.metric,
                    score: r.score,
                    reason: r.details?.['reason'] ?? '',
                  });
                }
              }
            }
          }
          this.failures.set(out);
        },
        error: (e: HttpErrorResponse) => {
          this.failLoading.set(false);
          this.failErr.set(
            `could not recompute per-trace results (${e.status || e.message})`);
        },
      });
    });
  }

  ngOnDestroy(): void { this.failSub?.unsubscribe(); }

  /** `null`/non-finite → honest 'n/a' — never a fabricated zero. */
  num(v: number | null | undefined): string | number {
    return v == null || !Number.isFinite(v) ? 'n/a' : v;
  }
}
