import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { ExperimentsComponent } from './experiments';
import { ExperimentDetail, ExperimentRow } from './api';

const mkExp = (id: string, over: Partial<ExperimentRow> = {}): ExperimentRow => ({
  experiment_id: id, project_id: 'default', name: `Exp ${id}`,
  status: 'open', result_ref: null, created_at: '2026-03-01T10:00:00Z',
  ...over,
});

const REPORT = {
  a: { dataset: 'ds-1@v1', trace_ids: ['t1', 't2'], n: 2 },
  b: { dataset: 'ds-1@v1', trace_ids: ['t3'], n: 1 },
  deltas: [{
    evaluator: 'error_free', metric: 'no_error_spans',
    n_a: 2, n_b: 1, mean_a: 1, mean_b: 0, delta: -1,
    pass_rate_a: 1, pass_rate_b: 0,
    significance: 'insufficient_data', p_value: null,
  }],
  multiple_comparisons: null,
};

const DETAIL: ExperimentDetail = {
  ...mkExp('exp-1', { status: 'completed', result_ref: 'run:1' }),
  description: 'baseline vs candidate',
  dataset_id: 'ds-1', dataset_version: 'v1',
  cohorts: {
    a: { trace_ids: ['t1', 't2'], label: 'baseline', config: {} },
    b: { trace_ids: ['t3'], label: 'candidate', config: { agent: 'v2' } },
  },
  evaluators: [{ name: 'error_free' }, { name: 'tool_success' }],
  provenance: 'imported',
  metadata: {},
  runs: [{
    run_id: 1, experiment_id: 'exp-1', project_id: 'default',
    run_at: '2026-03-02T10:00:00Z', report: REPORT,
  }],
  run_limit: 500, run_offset: 0,
};

const EVAL_RUN_EMPTY = {
  dataset: null, trace_ids: ['t1', 't2', 't3'], summary: {},
  results: {},
};

const listReq = (http: HttpTestingController) =>
  http.expectOne(r => r.method === 'GET' && r.url.endsWith('/experiments'));
const dsReq = (http: HttpTestingController) =>
  http.expectOne(r => r.method === 'GET' && r.url.endsWith('/datasets'));
const detailReq = (http: HttpTestingController, id: string) =>
  http.expectOne(r =>
    r.method === 'GET' && r.url.endsWith(`/experiments/${id}`));
const evalRunReq = (http: HttpTestingController) =>
  http.expectOne(r => r.method === 'POST' && r.url.endsWith('/eval/run'));
const btn = (el: HTMLElement, text: string) =>
  [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === text);

describe('ExperimentsComponent', () => {
  let http: HttpTestingController;
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [ExperimentsComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  const make = () => {
    const f = TestBed.createComponent(ExperimentsComponent);
    f.detectChanges();
    return f;
  };
  const flushList = (items: ExperimentRow[] = []) => {
    listReq(http).flush({ items, limit: 500, offset: 0 });
    dsReq(http).flush([]);   // datalist suggestions for the dataset pin
  };
  /** open an experiment whose detail carries one run — the auto-
   *  selected run fires the failure-examples recompute. */
  const openWithDetail = async (
    f: ReturnType<typeof make>, detail: ExperimentDetail = DETAIL) => {
    const el = f.nativeElement as HTMLElement;
    btn(el, detail.experiment_id)?.click();
    detailReq(http, detail.experiment_id).flush(detail);
    await f.whenStable();
    return el;
  };

  it('lists experiments with honest placeholders', async () => {
    const f = make();
    flushList([
      mkExp('exp-a', { name: 'Alpha' }),
      mkExp('exp-b', { name: null, created_at: null }),
    ]);
    await f.whenStable();
    const trs = (f.nativeElement as HTMLElement).querySelectorAll('tbody tr');
    expect(trs.length).toBe(2);
    expect(trs[0].textContent).toContain('exp-a');
    expect(trs[0].textContent).toContain('Alpha');
    expect(trs[0].textContent).toContain('open');
    expect(trs[1].textContent).toContain('—');   // null name/created
  });

  it('creates an experiment via PUT with the contract body', async () => {
    const f = make();
    flushList([]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const c = f.componentInstance;
    c.newId = 'exp-x'; c.newName = 'New Exp'; c.newDesc = 'a/b test';
    c.newDsId = 'ds-1'; c.newDsVer = 'v1';
    c.aLabel = 'baseline'; c.aIds = 't1, t2'; c.aCfg = '{"agent":"v1"}';
    c.bLabel = 'candidate'; c.bIds = 't3';
    btn(el, 'save experiment')?.click();
    const put = http.expectOne(r =>
      r.method === 'PUT' && r.url.endsWith('/experiments/exp-x'));
    expect(put.request.body).toEqual({
      name: 'New Exp',
      description: 'a/b test',
      dataset_id: 'ds-1', dataset_version: 'v1',
      cohorts: {
        a: { trace_ids: ['t1', 't2'], label: 'baseline',
             config: { agent: 'v1' } },
        b: { trace_ids: ['t3'], label: 'candidate' },
      },
      evaluators: [{ name: 'error_free' }, { name: 'tool_success' }],
    });
    put.flush({ experiment_id: 'exp-x', project_id: 'default', stored: true });
    listReq(http).flush({
      items: [mkExp('exp-x', { name: 'New Exp' })], limit: 500, offset: 0,
    });
    await f.whenStable();
    expect(el.querySelector('.ok-banner')?.textContent).toContain('exp-x');
    expect(el.querySelector('.err-banner')).toBeFalsy();
  });

  it('blocks a create with an empty cohort before PUT', async () => {
    const f = make();
    flushList([]);
    await f.whenStable();
    const c = f.componentInstance;
    c.newId = 'exp-x'; c.newName = 'X';
    c.aIds = ''; c.bIds = 't3';
    c.create();
    http.expectNone(r => r.url.endsWith('/experiments/exp-x'));
    expect(c.createErr()).toContain('cohort a');
    // dataset_version without dataset_id is the server's own rule
    c.aIds = 't1';
    c.newDsVer = 'v9';
    c.create();
    expect(c.createErr()).toContain('dataset_version requires dataset_id');
    http.expectNone(r => r.url.endsWith('/experiments/exp-x'));
  });

  it('opens an experiment: spec, run history, and the latest report', async () => {
    const f = make();
    flushList([mkExp('exp-1', { status: 'completed', result_ref: 'run:1' })]);
    await f.whenStable();
    const el = await openWithDetail(f);
    // spec summary + the auto-selected newest run
    expect(el.textContent).toContain('baseline vs candidate');
    expect(el.textContent).toContain('ds-1@v1');
    expect(el.textContent).toContain('completed');
    const runRows = el.querySelectorAll('section.panel table.runs tbody tr');
    expect(runRows.length).toBe(1);
    expect(runRows[0].textContent).toContain('run:1');
    expect(runRows[0].textContent).toContain('—');       // no gate sent
    expect(runRows[0].textContent).toContain('1 metric(s) · 0 significant');
    // the selected run's report renders through the shared view…
    await f.whenStable();
    const run = evalRunReq(http);                      // failure recompute
    expect(run.request.body).toEqual({
      trace_ids: ['t1', 't2', 't3'],
      evaluators: [{ name: 'error_free' }, { name: 'tool_success' }],
    });
    run.flush(EVAL_RUN_EMPTY);
    await f.whenStable();
    expect(el.textContent).toContain('run:1 report');
    expect(el.textContent).toContain('insufficient_data');
    expect(el.textContent).toContain('no failing eval results');
  });

  it('run now posts the gate, prepends the run, and renders failures', async () => {
    const f = make();
    flushList([mkExp('exp-1', { status: 'completed', result_ref: 'run:1' })]);
    await f.whenStable();
    const el = await openWithDetail(f);
    await f.whenStable();
    // run:1's recompute — a new run over identical ids+specs reuses it
    // (the recompute is deterministic)
    evalRunReq(http).flush({
      ...EVAL_RUN_EMPTY,
      results: {
        t3: [{
          evaluator: 'error_free', metric: 'no_error_spans', score: 0,
          details: { reason: 'span s9 has status error' },
        }],
      },
    });
    await f.whenStable();
    const c = f.componentInstance;
    c.runGate = true;
    btn(el, 'run now')?.click();
    const post = http.expectOne(r =>
      r.method === 'POST' && r.url.endsWith('/experiments/exp-1/run'));
    expect(post.request.body).toEqual({
      gate: { fail_on_significant_regression: true },
    });
    post.flush({
      experiment_id: 'exp-1', project_id: 'default',
      run_id: 2, run_at: '2026-03-03T10:00:00Z',
      report: {
        ...REPORT,
        deltas: [
          ...REPORT.deltas,
          { evaluator: 'token_budget', metric: 'within_token_budget',
            n_a: 2, n_b: 1, mean_a: 0.9, mean_b: 0.7, delta: -0.2,
            pass_rate_a: 0.9, pass_rate_b: 0.7,
            significance: 'insufficient_data', p_value: null },
        ],
        gate: {
          gate_passed: false,
          failures: [{
            metric: 'error_free/no_error_spans',
            reason: 'statistically significant regression',
            delta: -1, p_value: 0.01,
          }],
        },
      },
    });
    await f.whenStable();
    // the new run lands on top (newest-first) and is selected
    const runRows = el.querySelectorAll('section.panel table.runs tbody tr');
    expect(runRows[0].textContent).toContain('run:2');
    expect(runRows[0].textContent).toContain('failed');    // gate verdict
    expect(runRows[0].textContent).toContain('2 metric(s) · 0 significant');
    expect(el.textContent).toContain('run:2 report');
    expect(el.textContent).toContain('statistically significant regression');
    // experiment row + spec panel reflect the recorded lifecycle move
    expect(f.componentInstance.rows()[0].status).toBe('completed');
    expect(f.componentInstance.rows()[0].result_ref).toBe('run:2');
    // same ids+specs → the score-0 row stays shown (no refetch needed)
    http.expectNone(r => r.method === 'POST' && r.url.endsWith('/eval/run'));
    expect(el.textContent).toContain('span s9 has status error');
    expect(el.querySelector('a[href="/traces/t3"]')).toBeTruthy();
    // token metric + quality metric with means on both sides → pareto
    const pareto = el.querySelector('.pareto');
    expect(pareto).toBeTruthy();
    expect(pareto!.textContent).toContain('token_budget/within_token_budget');
    expect(pareto!.textContent).toContain('error_free/no_error_spans');
  });

  it('renders no pareto table when the report has no cost/token mean', async () => {
    const f = make();
    flushList([mkExp('exp-1')]);
    await f.whenStable();
    const el = await openWithDetail(f);
    await f.whenStable();
    evalRunReq(http).flush(EVAL_RUN_EMPTY);
    await f.whenStable();
    expect(el.querySelector('.pareto')).toBeNull();
  });

  it('surfaces the error banner when a run fails', async () => {
    const f = make();
    flushList([mkExp('exp-1')]);
    await f.whenStable();
    const el = await openWithDetail(f);
    await f.whenStable();
    evalRunReq(http).flush(EVAL_RUN_EMPTY);
    await f.whenStable();
    btn(el, 'run now')?.click();
    http.expectOne(r =>
      r.method === 'POST' && r.url.endsWith('/experiments/exp-1/run'))
      .flush({ detail: "trace 'gone' not found" },
        { status: 404, statusText: 'Not Found' });
    await f.whenStable();
    const banner = el.querySelector('.err-banner')?.textContent ?? '';
    expect(banner).toContain('run failed');
    expect(banner).toContain('gone');
  });

  it('shows the error banner when the list fails', async () => {
    const f = make();
    listReq(http).flush('down', { status: 500, statusText: 'Server Error' });
    dsReq(http).flush([]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelector('.err-banner')?.textContent).toContain('500');
    expect(f.componentInstance.loaded()).toBe(true);
  });
});
