import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { DatePipe, JsonPipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import {
  Api, CompareReport, CompareRequest, DatasetCaseInput, DatasetDetail,
  DatasetRow, DatasetVersionRow, EvalSpec,
} from './api';
import { CompareReportComponent } from './compare-report';
import { parseIds } from './ids';

/** Form-model for one case row — input/expected stay raw text until
 *  submit, when they must parse to JSON *objects* (the contract type). */
interface CaseFormRow { case_id: string; input: string; expected: string; }

const blankCase = (): CaseFormRow => ({ case_id: '', input: '', expected: '' });

@Component({
  selector: 'lens-datasets',
  imports: [FormsModule, DatePipe, JsonPipe, RouterLink, CompareReportComponent],
  styles: [`
    .ok-banner { color: var(--accent); border-radius: 4px; padding: .5rem .8rem;
      background: color-mix(in srgb, var(--accent) 12%, transparent); }
    .link { background: none; border: 0; color: var(--accent); cursor: pointer;
      padding: 0; font: inherit; text-align: left; }
    tr.sel td { background: color-mix(in srgb, var(--accent) 8%, transparent); }
    details.panel > summary { cursor: pointer; font-weight: 600; }
    textarea { font: inherit; background: var(--panel); color: var(--fg);
      border: 1px solid color-mix(in srgb, var(--fg) 25%, transparent);
      border-radius: 4px; padding: .25rem .5rem; width: 100%;
      box-sizing: border-box; }
    .cohorts { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem; }
    @media (max-width: 720px) { .cohorts { grid-template-columns: 1fr; } }
    .cohorts label { display: block; margin-bottom: .4rem; }
  `],
  template: `
  <h1>Datasets <a routerLink="/experiments" class="muted"
     style="font-size:.8em">experiments →</a></h1>
  <div class="filters">
    <input [ngModel]="search()" (ngModelChange)="search.set($event)"
           name="search" type="search" size="28"
           [ngModelOptions]="{ standalone: true }"
           placeholder="filter by dataset id or name…" aria-label="filter datasets" />
  </div>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (loaded() && filtered().length === 0) {
    <p class="muted">no datasets — create one below.</p> }
  <table>
    <thead><tr>
      <th>dataset</th><th>name</th><th>task</th><th>created</th>
    </tr></thead>
    <tbody>
      @for (d of visible(); track d.dataset_id) {
        <tr [class.sel]="selected()?.dataset_id === d.dataset_id">
          <td><button class="link" (click)="open(d)" [title]="d.dataset_id">{{ d.dataset_id }}</button></td>
          <td>{{ d.name }}</td>
          <td>{{ d.task ?? '—' }}</td>
          <td>{{ d.created_at ? (d.created_at | date:'yyyy-MM-dd HH:mm') : '—' }}</td>
        </tr>
      }
    </tbody>
  </table>
  @if (filtered().length > shown()) {
    <button (click)="showMore()">show more ({{ filtered().length - shown() }} hidden)</button> }

  <details class="panel" style="margin-top:.8rem">
    <summary>new dataset</summary>
    <p class="muted">PUT /v1/datasets/&lt;id&gt; — an existing id upserts its
      name/task metadata; versions are append-only.</p>
    <div class="filters" style="flex-wrap:wrap">
      <input [(ngModel)]="newId" [ngModelOptions]="{ standalone: true }"
             size="20" placeholder="dataset id" aria-label="new dataset id" />
      <input [(ngModel)]="newName" [ngModelOptions]="{ standalone: true }"
             size="24" placeholder="name (required)" aria-label="new dataset name" />
      <input [(ngModel)]="newTask" [ngModelOptions]="{ standalone: true }"
             size="16" placeholder="task (optional)" aria-label="new dataset task" />
      <button (click)="create()" [disabled]="creating()">
        {{ creating() ? 'saving…' : 'save dataset' }}</button>
    </div>
    @if (createErr()) { <p class="err-banner">{{ createErr() }}</p> }
    @if (createMsg()) { <p class="ok-banner">{{ createMsg() }}</p> }
  </details>

  @if (selected(); as sel) {
    <section class="panel" style="margin-top:.8rem">
      <h3>{{ sel.dataset_id }}</h3>
      @if (detailErr()) { <p class="err-banner">{{ detailErr() }}</p> }
      @if (detail(); as d) {
        <p class="muted">
          {{ d.name }} · task {{ d.task ?? '—' }} · project {{ d.project_id }}
          · created {{ d.created_at ? (d.created_at | date:'yyyy-MM-dd HH:mm:ss') : '—' }}
        </p>
        @if (hasMetadata(d)) { <pre>{{ d.metadata | json }}</pre> }
      } @else if (!detailErr()) { <p class="muted">loading…</p> }

      <h4>versions</h4>
      @if (versionsErr()) { <p class="err-banner">{{ versionsErr() }}</p> }
      @if (versions(); as vs) {
        @if (vs.length === 0) {
          <p class="muted">no versions — append one below.</p>
        } @else {
          <table>
            <thead><tr>
              <th>version</th><th>description</th><th>cases</th><th>created</th>
            </tr></thead>
            <tbody>
              @for (v of vs; track v.version) {
                <tr>
                  <td>{{ v.version }}</td>
                  <td>{{ v.description ?? '—' }}</td>
                  <td>{{ v.case_count }}</td>
                  <td>{{ v.created_at ? (v.created_at | date:'yyyy-MM-dd HH:mm') : '—' }}</td>
                </tr>
              }
            </tbody>
          </table>
        }
      } @else if (!versionsErr()) { <p class="muted">loading…</p> }

      <h4>new version <span class="muted">(append-only — an existing version is a 409)</span></h4>
      <div class="filters" style="flex-wrap:wrap">
        <input [(ngModel)]="verId" [ngModelOptions]="{ standalone: true }"
               size="10" placeholder="version" aria-label="new version" />
        <input [(ngModel)]="verDesc" [ngModelOptions]="{ standalone: true }"
               size="24" placeholder="description (optional)" aria-label="version description" />
      </div>
      @for (c of caseRows(); track $index; let i = $index) {
        <div class="filters">
          <input [(ngModel)]="c.case_id" [ngModelOptions]="{ standalone: true }"
                 size="12" placeholder="case_id" [aria-label]="'case id ' + (i + 1)" />
          <input [(ngModel)]="c.input" [ngModelOptions]="{ standalone: true }"
                 size="26" placeholder='input JSON object (optional)'
                 [aria-label]="'case input ' + (i + 1)" />
          <input [(ngModel)]="c.expected" [ngModelOptions]="{ standalone: true }"
                 size="26" placeholder='expected JSON object (optional)'
                 [aria-label]="'case expected ' + (i + 1)" />
          <button class="mini" (click)="removeCase(i)"
                  [disabled]="caseRows().length === 1"
                  [aria-label]="'remove case ' + (i + 1)">×</button>
        </div>
      }
      <div class="filters">
        <button (click)="addCase()">+ case</button>
        <button (click)="saveVersion()" [disabled]="savingVer()">
          {{ savingVer() ? 'saving…' : 'save version' }}</button>
      </div>
      @if (verErr()) { <p class="err-banner">{{ verErr() }}</p> }
      @if (verMsg()) { <p class="ok-banner">{{ verMsg() }}</p> }

      <details style="margin-top:.6rem">
        <summary><b>compare cohorts</b></summary>
        <p class="muted">POST /v1/eval/compare — each cohort needs the ids of
          stored traces (1–500; an unknown id is a 404). The dataset field on a
          side is only a label recorded on the report — it is not resolved to
          cases server-side.</p>
        <datalist id="ds-versions">
          @for (l of versionLabels(); track l) { <option [value]="l"></option> }
        </datalist>
        <div class="cohorts">
          <div>
            <label>a · dataset label
              <input [(ngModel)]="aDataset" [ngModelOptions]="{ standalone: true }"
                     list="ds-versions" placeholder="(unlabeled)" aria-label="cohort a dataset label" />
            </label>
            <label>a · trace ids
              <textarea [(ngModel)]="aIds" [ngModelOptions]="{ standalone: true }"
                        rows="3" aria-label="cohort a trace ids"
                        placeholder="comma / space / newline separated"></textarea>
            </label>
          </div>
          <div>
            <label>b · dataset label
              <input [(ngModel)]="bDataset" [ngModelOptions]="{ standalone: true }"
                     list="ds-versions" placeholder="(unlabeled)" aria-label="cohort b dataset label" />
            </label>
            <label>b · trace ids
              <textarea [(ngModel)]="bIds" [ngModelOptions]="{ standalone: true }"
                        rows="3" aria-label="cohort b trace ids"
                        placeholder="comma / space / newline separated"></textarea>
            </label>
          </div>
        </div>
        <div class="filters" style="flex-wrap:wrap; align-items:center">
          @for (n of evaluatorNames; track n) {
            <label><input type="checkbox" [checked]="evalSel().has(n)"
                          (change)="toggleEval(n)"> {{ n }}</label>
          }
        </div>
        <div class="filters" style="align-items:center">
          <label><input type="checkbox" [(ngModel)]="gateSig"
                        [ngModelOptions]="{ standalone: true }">
            gate: fail on a statistically significant regression</label>
          <button (click)="compare()" [disabled]="cmpRunning()">
            {{ cmpRunning() ? 'comparing…' : 'run compare' }}</button>
        </div>
        @if (cmpErr()) { <p class="err-banner">{{ cmpErr() }}</p> }
        @if (cmp(); as r) {
          <lens-compare-report [report]="r" [evaluators]="cmpSpecs()" />
        }
      </details>
    </section>
  }`,
})
export class DatasetsComponent implements OnInit {
  private api = inject(Api);
  rows = signal<DatasetRow[]>([]);
  search = signal('');
  loaded = signal(false);
  loading = signal(false);
  error = signal<string | null>(null);
  shown = signal(200);

  selected = signal<DatasetRow | null>(null);
  detail = signal<DatasetDetail | null>(null);
  detailErr = signal<string | null>(null);
  versions = signal<DatasetVersionRow[] | null>(null);
  versionsErr = signal<string | null>(null);

  // create-dataset form
  newId = ''; newName = ''; newTask = '';
  creating = signal(false);
  createErr = signal<string | null>(null);
  createMsg = signal<string | null>(null);

  // new-version form
  verId = ''; verDesc = '';
  caseRows = signal<CaseFormRow[]>([blankCase()]);
  savingVer = signal(false);
  verErr = signal<string | null>(null);
  verMsg = signal<string | null>(null);

  // compare-cohorts form
  aIds = ''; bIds = ''; aDataset = ''; bDataset = '';
  gateSig = false;
  cmpRunning = signal(false);
  cmpErr = signal<string | null>(null);
  cmp = signal<CompareReport | null>(null);
  /** The evaluator specs last posted to /eval/compare — the shared
   *  report view needs them to recompute per-trace failures. */
  cmpSpecs = signal<EvalSpec[]>([]);
  /** Mirrors forge_lens.eval.evaluators.EVALUATORS — specs run without
   *  params; param-required ones come back `score: null` + a reason. */
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
      r.dataset_id?.toLowerCase().includes(p) ||
      (r.name ?? '').toLowerCase().includes(p));
  });
  /** `dataset_id@version` labels the compare form offers per side. */
  versionLabels = computed(() =>
    (this.versions() ?? [])
      .map(v => `${v.dataset_id}@${v.version}`));
  visible = () => this.filtered().slice(0, this.shown());
  showMore(): void { this.shown.update(n => n + 500); }

  ngOnInit(): void { this.fetch(); }

  fetch(): void {
    if (this.loading()) return;
    this.loading.set(true);
    this.api.listDatasets().subscribe({
      next: page => {
        this.rows.set(page);
        this.loading.set(false); this.loaded.set(true);
      },
      error: (e: HttpErrorResponse) => {
        this.error.set(this.httpErr(e, 'datasets unavailable') +
          ' — is forge-lens serve running?');
        this.loading.set(false); this.loaded.set(true);
      },
    });
  }

  open(d: DatasetRow): void {
    this.selected.set(d);
    this.detail.set(null); this.detailErr.set(null);
    this.versions.set(null); this.versionsErr.set(null);
    this.cmp.set(null); this.cmpErr.set(null);
    this.verMsg.set(null); this.verErr.set(null);
    this.api.getDataset(d.dataset_id).subscribe({
      next: v => this.detail.set(v),
      error: (e: HttpErrorResponse) =>
        this.detailErr.set(this.httpErr(e, 'cannot load dataset')),
    });
    this.api.listDatasetVersions(d.dataset_id).subscribe({
      next: v => this.versions.set(v),
      error: (e: HttpErrorResponse) =>
        this.versionsErr.set(this.httpErr(e, 'cannot load versions')),
    });
  }

  create(): void {
    if (this.creating()) return;
    this.createErr.set(null); this.createMsg.set(null);
    const id = this.newId.trim(), name = this.newName.trim();
    const task = this.newTask.trim();
    if (!id) { this.createErr.set('dataset id required'); return; }
    if (!name) { this.createErr.set('name required — the server rejects a blank name'); return; }
    this.creating.set(true);
    const body: { name: string; task?: string } = { name };
    if (task) body.task = task;
    this.api.putDataset(id, body).subscribe({
      next: r => {
        this.creating.set(false);
        this.createMsg.set(`stored ${r.dataset_id} (project ${r.project_id})`);
        this.newId = ''; this.newName = ''; this.newTask = '';
        this.fetch();
      },
      error: (e: HttpErrorResponse) => {
        this.creating.set(false);
        this.createErr.set(this.httpErr(e, 'save failed'));
      },
    });
  }

  addCase(): void {
    this.caseRows.update(rs => [...rs, blankCase()]);
  }
  removeCase(i: number): void {
    this.caseRows.update(rs => rs.filter((_, j) => j !== i));
  }
  saveVersion(): void {
    const sel = this.selected();
    if (!sel || this.savingVer()) return;
    this.verErr.set(null); this.verMsg.set(null);
    const version = this.verId.trim();
    if (!version) { this.verErr.set('version string required'); return; }
    const cases: DatasetCaseInput[] = [];
    for (const [i, r] of this.caseRows().entries()) {
      const cid = r.case_id.trim();
      const input = this.parseObj(r.input, `case ${cid || i + 1} input`);
      if (input.err) { this.verErr.set(input.err); return; }
      const expected = this.parseObj(r.expected, `case ${cid || i + 1} expected`);
      if (expected.err) { this.verErr.set(expected.err); return; }
      if (!cid) {
        if (input.value !== undefined || expected.value !== undefined) {
          this.verErr.set(`case ${i + 1}: case_id required`); return;
        }
        continue; // untouched blank row — skipped
      }
      const c: DatasetCaseInput = { case_id: cid };
      if (input.value !== undefined) c.input = input.value;
      if (expected.value !== undefined) c.expected = expected.value;
      cases.push(c);
    }
    this.savingVer.set(true);
    const body: { cases: DatasetCaseInput[]; description?: string } = { cases };
    const desc = this.verDesc.trim();
    if (desc) body.description = desc;
    this.api.putDatasetVersion(sel.dataset_id, version, body).subscribe({
      next: r => {
        this.savingVer.set(false);
        this.verMsg.set(`stored version ${r.version} — ${r.cases} case(s)`);
        this.verId = ''; this.verDesc = '';
        this.caseRows.set([blankCase()]);
        this.api.listDatasetVersions(sel.dataset_id).subscribe({
          next: v => this.versions.set(v),
          error: () => { /* banner already shown on next manual refresh */ },
        });
      },
      error: (e: HttpErrorResponse) => {
        this.savingVer.set(false);
        this.verErr.set(this.httpErr(e, 'version save failed'));
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
  compare(): void {
    if (this.cmpRunning()) return;
    this.cmpErr.set(null);
    const a = parseIds(this.aIds), b = parseIds(this.bIds);
    if (!a.length) { this.cmpErr.set('cohort a: at least one trace id required'); return; }
    if (!b.length) { this.cmpErr.set('cohort b: at least one trace id required'); return; }
    const evaluators = [...this.evalSel()].sort().map(name => ({ name }));
    if (!evaluators.length) { this.cmpErr.set('select at least one evaluator'); return; }
    const body: CompareRequest = {
      a: { trace_ids: a }, b: { trace_ids: b }, evaluators,
    };
    const da = this.aDataset.trim(), db = this.bDataset.trim();
    if (da) body.a.dataset = da;
    if (db) body.b.dataset = db;
    if (this.gateSig) body.gate = { fail_on_significant_regression: true };
    this.cmpRunning.set(true); this.cmp.set(null);
    this.cmpSpecs.set(evaluators);
    this.api.runCompare(body).subscribe({
      next: r => { this.cmpRunning.set(false); this.cmp.set(r); },
      error: (e: HttpErrorResponse) => {
        this.cmpRunning.set(false);
        this.cmpErr.set(this.httpErr(e, 'compare failed'));
      },
    });
  }

  hasMetadata(d: DatasetDetail): boolean {
    return !!d.metadata && Object.keys(d.metadata).length > 0;
  }

  /** FastAPI rejections carry `{detail: str}` — surface it verbatim. */
  private httpErr(e: HttpErrorResponse, what: string): string {
    const d = (e.error as { detail?: unknown } | null)?.detail;
    const msg = typeof d === 'string' ? ` — ${d}` : '';
    return `${what} (${e.status || e.message})${msg}`;
  }
  /** Blank → absent; otherwise must parse to a JSON *object* (the
   *  DatasetCase contract — a bare string would be an invented shape). */
  private parseObj(raw: string, field: string):
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
}


