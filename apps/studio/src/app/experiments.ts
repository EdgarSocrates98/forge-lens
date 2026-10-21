import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe, JsonPipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import {
  Api, DatasetRow, EvalSpec, ExperimentCohort, ExperimentDetail,
  ExperimentRun, ExperimentRow, ExperimentSpecBody,
} from './api';
import { CompareReportComponent, normalizeCompare } from './compare-report';
import { parseIds } from './ids';

@Component({
  selector: 'lens-experiments',
  imports: [FormsModule, DatePipe, JsonPipe, RouterLink, CompareReportComponent],
  styles: [`
    .ok-banner { color: var(--accent); border-radius: 4px; padding: .5rem .8rem;
      background: color-mix(in srgb, var(--accent) 12%, transparent); }
    .link { background: none; border: 0; color: var(--accent); cursor: pointer;
      padding: 0; font: inherit; text-align: left; }
    tr.sel td { background: color-mix(in srgb, var(--accent) 8%, transparent); }
    details.panel > summary { cursor: pointer; font-weight: 600; }
    textarea, .cohorts input { font: inherit; background: var(--panel);
      color: var(--fg); border: 1px solid color-mix(in srgb, var(--fg) 25%, transparent);
      border-radius: 4px; padding: .25rem .5rem; width: 100%;
      box-sizing: border-box; }
    .cohorts { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; }
    @media (max-width: 720px) { .cohorts { grid-template-columns: 1fr; } }
    .cohorts label { display: block; margin-bottom: .4rem; }
  `],
  template: `
  <h1>Experiments <a routerLink="/datasets" class="muted"
     style="font-size:.8em">datasets →</a></h1>
  <p class="muted">Persisted A/B eval specs — each run appends to an
    append-only history through the same path as POST /v1/eval/compare.</p>
  <div class="filters">
    <input [ngModel]="search()" (ngModelChange)="search.set($event)"
           name="search" type="search" size="28"
           [ngModelOptions]="{ standalone: true }"
           placeholder="filter by experiment id or name…"
           aria-label="filter experiments" />
  </div>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (loaded() && filtered().length === 0) {
    <p class="muted">no experiments — create one below.</p> }
  <table>
    <thead><tr>
      <th>experiment</th><th>name</th><th>status</th>
      <th>last run</th><th>created</th>
    </tr></thead>
    <tbody>
      @for (e of visible(); track e.experiment_id) {
        <tr [class.sel]="selected()?.experiment_id === e.experiment_id">
          <td><button class="link" (click)="open(e)"
                      [title]="e.experiment_id">{{ e.experiment_id }}</button></td>
          <td>{{ e.name ?? '—' }}</td>
          <td>{{ e.status }}</td>
          <td><code>{{ e.result_ref ?? '—' }}</code></td>
          <td>{{ e.created_at ? (e.created_at | date:'yyyy-MM-dd HH:mm') : '—' }}</td>
        </tr>
      }
    </tbody>
  </table>
  @if (filtered().length > shown()) {
    <button (click)="showMore()">show more
      ({{ filtered().length - shown() }} hidden)</button> }

  <details class="panel" style="margin-top:.8rem">
    <summary>new experiment</summary>
    <p class="muted">PUT /v1/experiments/&lt;id&gt; — an existing id
      upserts the spec; runs stay append-only. Both cohorts pin stored
      trace ids (1–500); a cohort's label/config are recorded verbatim,
      never resolved.</p>
    <div class="filters" style="flex-wrap:wrap">
      <input [(ngModel)]="newId" [ngModelOptions]="{ standalone: true }"
             size="16" placeholder="experiment id" aria-label="new experiment id" />
      <input [(ngModel)]="newName" [ngModelOptions]="{ standalone: true }"
             size="22" placeholder="name (required)" aria-label="new experiment name" />
      <input [(ngModel)]="newDesc" [ngModelOptions]="{ standalone: true }"
             size="24" placeholder="description (optional)"
             aria-label="new experiment description" />
    </div>
    <div class="filters" style="flex-wrap:wrap">
      <input [(ngModel)]="newDsId" [ngModelOptions]="{ standalone: true }"
             (change)="onDsPick()" list="exp-datasets" size="18"
             placeholder="dataset_id (optional)" aria-label="pinned dataset id" />
      <input [(ngModel)]="newDsVer" [ngModelOptions]="{ standalone: true }"
             list="exp-ds-versions" size="14"
             placeholder="version (needs dataset_id)" aria-label="pinned dataset version" />
      <datalist id="exp-datasets">
        @for (d of dsRows(); track d.dataset_id) {
          <option [value]="d.dataset_id"></option> }
      </datalist>
      <datalist id="exp-ds-versions">
        @for (v of dsVersions(); track v) { <option [value]="v"></option> }
      </datalist>
      <span class="muted">pinned truth → the report's dataset label
        (id&#64;version)</span>
    </div>
    <div class="cohorts">
      <div>
        <label>a · label
          <input [(ngModel)]="aLabel" [ngModelOptions]="{ standalone: true }"
                 placeholder="baseline" aria-label="cohort a label" /></label>
        <label>a · trace ids
          <textarea [(ngModel)]="aIds" [ngModelOptions]="{ standalone: true }"
                    rows="3" aria-label="cohort a trace ids"
                    placeholder="comma / space / newline separated"></textarea></label>
        <label>a · config JSON (optional)
          <input [(ngModel)]="aCfg" [ngModelOptions]="{ standalone: true }"
                 placeholder='{"agent":"v1"}' aria-label="cohort a config" /></label>
      </div>
      <div>
        <label>b · label
          <input [(ngModel)]="bLabel" [ngModelOptions]="{ standalone: true }"
                 placeholder="candidate" aria-label="cohort b label" /></label>
        <label>b · trace ids
          <textarea [(ngModel)]="bIds" [ngModelOptions]="{ standalone: true }"
                    rows="3" aria-label="cohort b trace ids"
                    placeholder="comma / space / newline separated"></textarea></label>
        <label>b · config JSON (optional)
          <input [(ngModel)]="bCfg" [ngModelOptions]="{ standalone: true }"
                 placeholder='{"agent":"v2"}' aria-label="cohort b config" /></label>
      </div>
    </div>
    <div class="filters" style="flex-wrap:wrap; align-items:center">
      @for (n of evaluatorNames; track n) {
        <label><input type="checkbox" [checked]="evalSel().has(n)"
                      (change)="toggleEval(n)"> {{ n }}</label>
      }
      <button (click)="create()" [disabled]="creating()">
        {{ creating() ? 'saving…' : 'save experiment' }}</button>
    </div>
    @if (createErr()) { <p class="err-banner">{{ createErr() }}</p> }
    @if (createMsg()) { <p class="ok-banner">{{ createMsg() }}</p> }
  </details>

  @if (selected(); as sel) {
    <section class="panel" style="margin-top:.8rem">
      <h3>{{ sel.experiment_id }}</h3>
      @if (detailErr()) { <p class="err-banner">{{ detailErr() }}</p> }
      @if (detail(); as d) {
        <p class="muted">
          {{ d.name ?? '—' }} · status <b>{{ d.status }}</b> ·
          project {{ d.project_id }} ·
          created {{ d.created_at ? (d.created_at | date:'yyyy-MM-dd HH:mm:ss') : '—' }}
          @if (d.dataset_id) {
            · dataset {{ d.dataset_id }}{{ d.dataset_version ? '@' + d.dataset_version : '' }} }
          @if (d.result_ref) { · last run <code>{{ d.result_ref }}</code> }
        </p>
        @if (d.description) { <p class="muted">{{ d.description }}</p> }
        <p class="muted">
          a: {{ d.cohorts?.a?.trace_ids?.length ?? 0 }} trace(s)
          @if (d.cohorts?.a?.label; as la) { · {{ la }} } ·
          b: {{ d.cohorts?.b?.trace_ids?.length ?? 0 }} trace(s)
          @if (d.cohorts?.b?.label; as lb) { · {{ lb }} } ·
          evaluators:
          @if ((d.evaluators ?? []).length === 0) { — }
          @for (e of d.evaluators ?? []; track $index; let last = $last) {
            <code>{{ e.name }}</code>{{ last ? '' : ' ' }} }
        </p>
        @if (hasMetadata(d)) { <pre>{{ d.metadata | json }}</pre> }
      } @else if (!detailErr()) { <p class="muted">loading…</p> }

      <h4>runs <span class="muted">(append-only, newest first)</span></h4>
      <div class="filters" style="align-items:center">
        <label><input type="checkbox" [(ngModel)]="runGate"
                      [ngModelOptions]="{ standalone: true }">
          gate: fail on a statistically significant regression</label>
        <button (click)="runNow()" [disabled]="running()">
          {{ running() ? 'running…' : 'run now' }}</button>
      </div>
      @if (runErr()) { <p class="err-banner">{{ runErr() }}</p> }
      @if (runsDesc().length === 0) {
        <p class="muted">no runs yet — run now executes the stored spec
          and appends the report here.</p>
      } @else {
        <table class="runs">
          <thead><tr>
            <th>run</th><th>when</th><th>gate</th><th>deltas</th>
          </tr></thead>
          <tbody>
            @for (r of runsDesc(); track r.run_id) {
              <tr [class.sel]="selRunId() === r.run_id">
                <td><button class="link" (click)="selRunId.set(r.run_id)">
                  run:{{ r.run_id }}</button></td>
                <td>{{ r.run_at ? (r.run_at | date:'yyyy-MM-dd HH:mm:ss') : '—' }}</td>
                <td>{{ gateOf(r) }}</td>
                <td>{{ deltaSummary(r) }}</td>
              </tr>
            }
          </tbody>
        </table>
      }
      @if (selRun(); as run) {
        <h4>run:{{ run.run_id }} report</h4>
        <lens-compare-report [report]="run.report"
                             [evaluators]="specEvals()" />
      }
    </section>
  }`,
})
export class ExperimentsComponent implements OnInit {
  private api = inject(Api);
  rows = signal<ExperimentRow[]>([]);
  search = signal('');
  loaded = signal(false);
  loading = signal(false);
  error = signal<string | null>(null);
  shown = signal(200);

  selected = signal<ExperimentRow | null>(null);
  detail = signal<ExperimentDetail | null>(null);
  detailErr = signal<string | null>(null);

  // create-experiment form
  newId = ''; newName = ''; newDesc = '';
  newDsId = ''; newDsVer = '';
  aLabel = ''; aIds = ''; aCfg = '';
  bLabel = ''; bIds = ''; bCfg = '';
  creating = signal(false);
  createErr = signal<string | null>(null);
  createMsg = signal<string | null>(null);
  /** Datasets/versions offered to the dataset pin — datalist
   *  suggestions only; the fields stay free-text. */
  dsRows = signal<DatasetRow[]>([]);
  dsVersions = signal<string[]>([]);

  // run section
  runGate = false;
  running = signal(false);
  runErr = signal<string | null>(null);
  selRunId = signal<number | null>(null);

  /** Mirrors forge_lens.eval.evaluators.EVALUATORS — specs store as
   *  `{name}` (params unsupported here; param-required evaluators come
   *  back `score: null` + a reason). */
  readonly evaluatorNames = [
    'allowed_tools', 'claims_grounded', 'contains', 'error_free',
    'evidence_coverage', 'exact_match', 'has_span', 'human_review',
    'latency_budget', 'precision_recall', 'regex', 'schema_validation',
    'task_completion', 'token_budget', 'token_f1', 'tool_call_count',
    'tool_success',
  ];
  evalSel = signal<Set<string>>(new Set(['error_free', 'tool_success']));

  /** client-side substring filter over the fetched list */
  filtered = computed(() => {
    const p = this.search().trim().toLowerCase();
    const all = this.rows();
    if (!p) return all;
    return all.filter(r =>
      r.experiment_id?.toLowerCase().includes(p) ||
      (r.name ?? '').toLowerCase().includes(p));
  });
  visible = () => this.filtered().slice(0, this.shown());
  showMore(): void { this.shown.update(n => n + 500); }

  /** Stored order is run_id ASC — render newest first so a fresh run
   *  prepends where the eye lands. */
  runsDesc = computed(() =>
    (this.detail()?.runs ?? []).slice().sort((a, b) => b.run_id - a.run_id));
  selRun = computed(() =>
    (this.detail()?.runs ?? []).find(r => r.run_id === this.selRunId()) ?? null);
  /** The evaluators on the stored spec — what a `run now` executes.
   *  Passed to the report view for the failure recompute (a run made
   *  before a re-spec was evaluated with the spec of its time; the
   *  recompute is labeled as such there). */
  specEvals = computed<EvalSpec[]>(() =>
    (this.detail()?.evaluators ?? [])
      .map(e => ({ name: e.name, ...(e.params ? { params: e.params } : {}) })));

  ngOnInit(): void {
    this.fetch();
    // datalist suggestions for the dataset pin, loaded once — a
    // failure only means no suggestions, never an extra banner
    // competing with the list
    this.api.listDatasets().subscribe({
      next: ds => this.dsRows.set(ds),
      error: () => this.dsRows.set([]),
    });
  }

  fetch(): void {
    if (this.loading()) return;
    this.loading.set(true);
    this.api.listExperiments().subscribe({
      next: page => {
        this.rows.set(page.items ?? []);
        this.loading.set(false); this.loaded.set(true);
      },
      error: (e: HttpErrorResponse) => {
        this.error.set(this.httpErr(e, 'experiments unavailable') +
          ' — is forge-lens serve running?');
        this.loading.set(false); this.loaded.set(true);
      },
    });
  }

  onDsPick(): void {
    const id = this.newDsId.trim();
    this.dsVersions.set([]);
    if (!id) return;
    this.api.listDatasetVersions(id).subscribe({
      next: vs => this.dsVersions.set(vs.map(v => v.version)),
      error: () => this.dsVersions.set([]),
    });
  }

  open(e: ExperimentRow): void {
    this.selected.set(e);
    this.detail.set(null); this.detailErr.set(null);
    this.runErr.set(null);
    this.selRunId.set(null);
    this.api.getExperiment(e.experiment_id).subscribe({
      next: d => {
        this.detail.set(d);
        // default to the newest recorded run, if any
        const latest = (d.runs ?? [])
          .reduce<number | null>((m, r) =>
            m === null || r.run_id > m ? r.run_id : m, null);
        this.selRunId.set(latest);
      },
      error: (err: HttpErrorResponse) =>
        this.detailErr.set(this.httpErr(err, 'cannot load experiment')),
    });
  }

  toggleEval(name: string): void {
    this.evalSel.update(sel => {
      const next = new Set(sel);
      if (next.has(name)) next.delete(name); else next.add(name);
      return next;
    });
  }

  create(): void {
    if (this.creating()) return;
    this.createErr.set(null); this.createMsg.set(null);
    const id = this.newId.trim(), name = this.newName.trim();
    if (!id) { this.createErr.set('experiment id required'); return; }
    if (!name) {
      this.createErr.set('name required — the server rejects a blank name');
      return;
    }
    const a = parseIds(this.aIds), b = parseIds(this.bIds);
    if (!a.length) {
      this.createErr.set('cohort a: at least one trace id required'); return;
    }
    if (!b.length) {
      this.createErr.set('cohort b: at least one trace id required'); return;
    }
    const aCfg = parseJsonObj(this.aCfg, 'cohort a config');
    if (aCfg.err) { this.createErr.set(aCfg.err); return; }
    const bCfg = parseJsonObj(this.bCfg, 'cohort b config');
    if (bCfg.err) { this.createErr.set(bCfg.err); return; }
    const evaluators: EvalSpec[] =
      [...this.evalSel()].sort().map(n => ({ name: n }));
    if (!evaluators.length) {
      this.createErr.set('select at least one evaluator'); return;
    }
    const dsId = this.newDsId.trim(), dsVer = this.newDsVer.trim();
    if (dsVer && !dsId) {
      this.createErr.set('dataset_version requires dataset_id'); return;
    }
    const side = (ids: string[], label: string,
      cfg: { value?: Record<string, unknown> }): ExperimentCohort => {
      const c: ExperimentCohort = { trace_ids: ids };
      const l = label.trim();
      if (l) c.label = l;
      if (cfg.value !== undefined) c.config = cfg.value;
      return c;
    };
    const body: ExperimentSpecBody = {
      name,
      cohorts: { a: side(a, this.aLabel, aCfg), b: side(b, this.bLabel, bCfg) },
      evaluators,
    };
    const desc = this.newDesc.trim();
    if (desc) body.description = desc;
    if (dsId) { body.dataset_id = dsId; if (dsVer) body.dataset_version = dsVer; }
    this.creating.set(true);
    this.api.putExperiment(id, body).subscribe({
      next: r => {
        this.creating.set(false);
        this.createMsg.set(`stored ${r.experiment_id} (project ${r.project_id})`);
        this.newId = ''; this.newName = ''; this.newDesc = '';
        this.newDsId = ''; this.newDsVer = '';
        this.aLabel = ''; this.aIds = ''; this.aCfg = '';
        this.bLabel = ''; this.bIds = ''; this.bCfg = '';
        this.fetch();
      },
      error: (e: HttpErrorResponse) => {
        this.creating.set(false);
        this.createErr.set(this.httpErr(e, 'save failed'));
      },
    });
  }

  runNow(): void {
    const sel = this.selected();
    if (!sel || this.running()) return;
    this.runErr.set(null);
    this.running.set(true);
    const gate = this.runGate
      ? { fail_on_significant_regression: true } : undefined;
    this.api.runExperiment(sel.experiment_id, gate).subscribe({
      next: r => {
        this.running.set(false);
        const run: ExperimentRun = {
          run_id: r.run_id, experiment_id: r.experiment_id,
          project_id: r.project_id, run_at: r.run_at, report: r.report,
        };
        // the run is recorded server-side in the same transaction —
        // prepending mirrors the persisted history, and the experiment
        // row advances its honest lifecycle fields
        this.detail.update(d => d
          ? { ...d, status: 'completed', result_ref: `run:${r.run_id}`,
              runs: [...(d.runs ?? []), run] }
          : d);
        this.rows.update(rs => rs.map(e =>
          e.experiment_id === sel.experiment_id
            ? { ...e, status: 'completed', result_ref: `run:${r.run_id}` }
            : e));
        this.selRunId.set(r.run_id);
      },
      error: (e: HttpErrorResponse) => {
        this.running.set(false);
        this.runErr.set(this.httpErr(e, 'run failed'));
      },
    });
  }

  /** Gate verdict from the run's own report — '—' when no gate ran. */
  gateOf(r: ExperimentRun): string {
    const g = normalizeCompare(r.report).gate;
    return g === undefined ? '—' : g.gate_passed ? 'passed' : 'failed';
  }
  /** `N metrics · M significant` — counts from the stored deltas. */
  deltaSummary(r: ExperimentRun): string {
    const ds = normalizeCompare(r.report).deltas;
    if (!ds.length) return 'no deltas';
    const sig = ds.filter(d => d.significance === 'significant').length;
    return `${ds.length} metric(s) · ${sig} significant`;
  }

  hasMetadata(d: ExperimentDetail): boolean {
    return !!d.metadata && Object.keys(d.metadata).length > 0;
  }

  /** FastAPI rejections carry `{detail: str}` — surface it verbatim. */
  private httpErr(e: HttpErrorResponse, what: string): string {
    const d = (e.error as { detail?: unknown } | null)?.detail;
    const msg = typeof d === 'string' ? ` — ${d}` : '';
    return `${what} (${e.status || e.message})${msg}`;
  }
}

/** Blank → absent; otherwise must parse to a JSON *object* (the
 *  cohort `config` contract — a bare string is an invented shape). */
function parseJsonObj(raw: string, field: string):
  { value?: Record<string, unknown>; err?: string } {
  const t = raw.trim();
  if (!t) return {};
  try {
    const v: unknown = JSON.parse(t);
    if (v === null || typeof v !== 'object' || Array.isArray(v)) {
      return { err: `${field}: a JSON object is required` };
    }
    return { value: v as Record<string, unknown> };
  } catch {
    return { err: `${field}: invalid JSON` };
  }
}
