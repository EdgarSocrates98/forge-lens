import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { DatasetsComponent } from './datasets';
import { DatasetRow, DatasetVersionRow } from './api';

const mkDs = (id: string, over: Partial<DatasetRow> = {}): DatasetRow => ({
  dataset_id: id, project_id: 'default', name: `DS ${id}`,
  task: 'support-bot', created_at: '2026-03-01T10:00:00Z',
  ...over,
});
const mkVer = (v: string, over: Partial<DatasetVersionRow> = {}): DatasetVersionRow => ({
  dataset_id: 'ds-1', project_id: 'default', version: v,
  description: null, case_count: 3, created_at: '2026-03-02T10:00:00Z',
  ...over,
});

const listReq = (http: HttpTestingController) =>
  http.expectOne(r => r.method === 'GET' && r.url.endsWith('/datasets'));
const detailReq = (http: HttpTestingController, id: string) =>
  http.expectOne(r => r.method === 'GET' && r.url.endsWith(`/datasets/${id}`));
const versionsReq = (http: HttpTestingController, id: string) =>
  http.expectOne(r => r.method === 'GET' && r.url.endsWith(`/datasets/${id}/versions`));
const btn = (el: HTMLElement, text: string) =>
  [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === text);
const rows = (el: HTMLElement) => el.querySelectorAll('tbody tr');

describe('DatasetsComponent', () => {
  let http: HttpTestingController;
  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [DatasetsComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  it('renders dataset rows with honest placeholders for nulls', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([
      mkDs('ds-a', { name: 'Alpha' }),
      mkDs('ds-b', { task: null, created_at: null }),
    ]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const trs = rows(el);
    expect(trs.length).toBe(2);
    expect(trs[0].textContent).toContain('ds-a');
    expect(trs[0].textContent).toContain('Alpha');
    expect(trs[0].textContent).toContain('support-bot');
    expect(trs[1].textContent).toContain('—'); // null task / created_at
  });

  it('creates a dataset via PUT with the contract body', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const c = f.componentInstance;
    c.newId = 'ds-x'; c.newName = 'Experiment X'; c.newTask = 'support';
    btn(el, 'save dataset')?.click();
    const put = http.expectOne(r =>
      r.method === 'PUT' && r.url.endsWith('/datasets/ds-x'));
    expect(put.request.body).toEqual({ name: 'Experiment X', task: 'support' });
    put.flush({ dataset_id: 'ds-x', project_id: 'default', stored: true });
    listReq(http).flush([mkDs('ds-x', { name: 'Experiment X' })]);
    await f.whenStable();
    expect(el.querySelector('.ok-banner')?.textContent).toContain('ds-x');
    expect(el.querySelector('.err-banner')).toBeFalsy();
    expect(rows(el).length).toBe(1);
  });

  it('shows the error banner when the list fails', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush('down', { status: 500, statusText: 'Server Error' });
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelector('.err-banner')?.textContent).toContain('500');
    expect(f.componentInstance.loaded()).toBe(true);
  });

  it('opens a dataset, loads detail + versions, and saves a version', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([mkDs('ds-1')]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'ds-1')?.click();
    detailReq(http, 'ds-1').flush({
      ...mkDs('ds-1'), metadata: { owner: 'eval-team' },
    });
    versionsReq(http, 'ds-1').flush([mkVer('v1'), mkVer('v2')]);
    await f.whenStable();
    expect(el.textContent).toContain('v1');
    expect(el.textContent).toContain('v2');

    const c = f.componentInstance;
    c.verId = 'v3'; c.verDesc = 'third cut';
    c.caseRows.set([
      { case_id: 'c1', input: '{"q":"hi"}', expected: '{"a":"hello"}' },
    ]);
    const save = [...el.querySelectorAll('button')]
      .find(b => b.textContent?.includes('save version'));
    save?.click();
    const put = http.expectOne(r =>
      r.method === 'PUT' && r.url.endsWith('/datasets/ds-1/versions/v3'));
    expect(put.request.body).toEqual({
      cases: [{ case_id: 'c1', input: { q: 'hi' }, expected: { a: 'hello' } }],
      description: 'third cut',
    });
    put.flush({ dataset_id: 'ds-1', version: 'v3', cases: 1 });
    versionsReq(http, 'ds-1').flush([mkVer('v1'), mkVer('v2'), mkVer('v3')]);
    await f.whenStable();
    expect(el.querySelector('.ok-banner')?.textContent).toContain('v3');
    expect(el.textContent).toContain('v3');
  });

  it('surfaces a 409 when the version already exists', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([mkDs('ds-1')]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'ds-1')?.click();
    detailReq(http, 'ds-1').flush({ ...mkDs('ds-1'), metadata: {} });
    versionsReq(http, 'ds-1').flush([mkVer('v1')]);
    await f.whenStable();
    const c = f.componentInstance;
    c.verId = 'v1';
    c.caseRows.set([{ case_id: 'c9', input: '', expected: '' }]);
    [...el.querySelectorAll('button')]
      .find(b => b.textContent?.includes('save version'))?.click();
    http.expectOne(r =>
      r.method === 'PUT' && r.url.endsWith('/datasets/ds-1/versions/v1'))
      .flush({ detail: "dataset version 'ds-1'@'v1' exists" },
        { status: 409, statusText: 'Conflict' });
    await f.whenStable();
    const banner = el.querySelector('.err-banner')?.textContent ?? '';
    expect(banner).toContain('409');
    expect(banner).toContain('exists');
  });

  it('posts the real compare body and renders deltas + gate verdict', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([mkDs('ds-1')]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'ds-1')?.click();
    detailReq(http, 'ds-1').flush({ ...mkDs('ds-1'), metadata: {} });
    versionsReq(http, 'ds-1').flush([mkVer('v1'), mkVer('v2')]);
    await f.whenStable();

    const c = f.componentInstance;
    c.aIds = 't1, t2'; c.bIds = 't3';
    c.aDataset = 'ds-1@v1'; c.bDataset = 'ds-1@v2';
    c.gateSig = true;
    [...el.querySelectorAll('button')]
      .find(b => b.textContent?.includes('run compare'))?.click();
    const post = http.expectOne(r =>
      r.method === 'POST' && r.url.endsWith('/eval/compare'));
    expect(post.request.body).toEqual({
      a: { trace_ids: ['t1', 't2'], dataset: 'ds-1@v1' },
      b: { trace_ids: ['t3'], dataset: 'ds-1@v2' },
      evaluators: [{ name: 'error_free' }, { name: 'tool_success' }],
      gate: { fail_on_significant_regression: true },
    });
    post.flush({
      a: { dataset: 'ds-1@v1', trace_ids: ['t1', 't2'], n: 2 },
      b: { dataset: 'ds-1@v2', trace_ids: ['t3'], n: 1 },
      deltas: [{
        evaluator: 'error_free', metric: 'no_error_spans',
        n_a: 2, n_b: 1, mean_a: 1, mean_b: 0, delta: -1,
        pass_rate_a: 1, pass_rate_b: 0,
        significance: 'insufficient_data', p_value: null,
      }],
      gate: {
        gate_passed: false,
        failures: [{
          metric: 'error_free/no_error_spans',
          reason: 'statistically significant regression',
          delta: -1, p_value: 0.01,
        }],
      },
    });
    await f.whenStable();
    // the report view recomputes per-trace results for the failure
    // examples table — same evaluators over the cohort union
    const evalRun = http.expectOne(r =>
      r.method === 'POST' && r.url.endsWith('/eval/run'));
    expect(evalRun.request.body).toEqual({
      trace_ids: ['t1', 't2', 't3'],
      evaluators: [{ name: 'error_free' }, { name: 'tool_success' }],
    });
    evalRun.flush({
      dataset: null, trace_ids: ['t1', 't2', 't3'], summary: {},
      results: {
        t3: [{
          evaluator: 'error_free', metric: 'no_error_spans', score: 0,
          details: { reason: 'span s9 has status error' },
        }],
      },
    });
    await f.whenStable();
    const text = el.textContent ?? '';
    expect(text).toContain('insufficient_data');
    expect(text).toContain('n/a');           // p_value: null, honest
    expect(text).toContain('failed');        // gate verdict
    expect(text).toContain('statistically significant regression');
    expect(text).toContain('ds-1@v1');
    // failure examples — the score-0 row with its trace link
    expect(text).toContain('failure examples');
    expect(text).toContain('span s9 has status error');
    expect(el.querySelector('a[href="/traces/t3"]')).toBeTruthy();
    // no cost/token metric in this report → no pareto table
    expect(el.querySelector('.pareto')).toBeNull();
  });

  it('renders the quality-vs-cost table when both metric classes carry means', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([mkDs('ds-1')]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'ds-1')?.click();
    detailReq(http, 'ds-1').flush({ ...mkDs('ds-1'), metadata: {} });
    versionsReq(http, 'ds-1').flush([]);
    await f.whenStable();
    const c = f.componentInstance;
    c.aIds = 't1'; c.bIds = 't2';
    c.evalSel.set(new Set(['token_budget']));
    [...el.querySelectorAll('button')]
      .find(b => b.textContent?.includes('run compare'))?.click();
    http.expectOne(r => r.method === 'POST' && r.url.endsWith('/eval/compare'))
      .flush({
        a: { dataset: null, trace_ids: ['t1'], n: 1 },
        b: { dataset: null, trace_ids: ['t2'], n: 1 },
        deltas: [
          { evaluator: 'error_free', metric: 'no_error_spans',
            n_a: 1, n_b: 1, mean_a: 1, mean_b: 0.5, delta: -0.5,
            pass_rate_a: 1, pass_rate_b: 0.5,
            significance: 'insufficient_data', p_value: null },
          { evaluator: 'token_budget', metric: 'within_token_budget',
            n_a: 1, n_b: 1, mean_a: 0.9, mean_b: 0.7, delta: -0.2,
            pass_rate_a: 0.9, pass_rate_b: 0.7,
            significance: 'insufficient_data', p_value: null },
        ],
      });
    await f.whenStable();
    http.expectOne(r => r.method === 'POST' && r.url.endsWith('/eval/run'))
      .flush({ dataset: null, trace_ids: ['t1', 't2'], summary: {},
               results: {} });
    await f.whenStable();
    const pareto = el.querySelector('.pareto');
    expect(pareto).toBeTruthy();
    const text = pareto!.textContent ?? '';
    // honest labels — the metric names themselves, no invented axes
    expect(text).toContain('error_free/no_error_spans');
    expect(text).toContain('token_budget/within_token_budget');
    expect(text).toContain('0.9');
    expect(text).toContain('0.7');
    // empty per-trace recompute → honest empty state
    expect(el.textContent).toContain('no failing eval results');
  });

  it('shows the compare error banner on a 404 trace', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([mkDs('ds-1')]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'ds-1')?.click();
    detailReq(http, 'ds-1').flush({ ...mkDs('ds-1'), metadata: {} });
    versionsReq(http, 'ds-1').flush([]);
    await f.whenStable();
    const c = f.componentInstance;
    c.aIds = 'gone-1'; c.bIds = 'gone-2';
    [...el.querySelectorAll('button')]
      .find(b => b.textContent?.includes('run compare'))?.click();
    http.expectOne(r => r.method === 'POST' && r.url.endsWith('/eval/compare'))
      .flush({ detail: "trace 'gone-1' not found" },
        { status: 404, statusText: 'Not Found' });
    await f.whenStable();
    const banner = el.querySelector('.err-banner')?.textContent ?? '';
    expect(banner).toContain('compare failed');
    expect(banner).toContain('gone-1');
  });

  it('opens a version and renders its stored cases', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([mkDs('ds-1')]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'ds-1')?.click();
    detailReq(http, 'ds-1').flush({ ...mkDs('ds-1'), metadata: {} });
    versionsReq(http, 'ds-1').flush([mkVer('v1'), mkVer('v2')]);
    await f.whenStable();
    // clicking the version row fetches GET /datasets/{id}/versions/{v}
    btn(el, 'v1')?.click();
    const verReq = http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/datasets/ds-1/versions/v1'));
    // mirrors SQLiteTraceStore.get_dataset_version — version fields +
    // the cases list ordered by case_id
    verReq.flush({
      dataset_id: 'ds-1', project_id: 'default', version: 'v1',
      description: 'first cut', created_at: '2026-03-02T10:00:00Z',
      metadata: {}, cases: [
        { case_id: 'c1', input: { q: 'hi' }, expected: { a: 'hello' },
          allowed_tools: ['search'], constraints: {}, rubric: {} },
        { case_id: 'c2', input: { q: 'bye' }, expected: { a: 'bye' },
          allowed_tools: [], constraints: {}, rubric: { strict: true } },
      ],
    });
    await f.whenStable();
    const cases = el.querySelector('table.cases')!;
    const trs = cases.querySelectorAll('tbody tr');
    expect(trs.length).toBe(2);
    expect(trs[0].textContent).toContain('c1');
    expect(trs[0].textContent).toContain('"hi"');
    expect(trs[0].textContent).toContain('"hello"');
    expect(trs[0].textContent).toContain('search');
    expect(trs[1].textContent).toContain('c2');
    expect(trs[1].textContent).toContain('"strict"');
  });

  it('surfaces the error banner when a version fetch 404s', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([mkDs('ds-1')]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    btn(el, 'ds-1')?.click();
    detailReq(http, 'ds-1').flush({ ...mkDs('ds-1'), metadata: {} });
    versionsReq(http, 'ds-1').flush([mkVer('v1')]);
    await f.whenStable();
    btn(el, 'v1')?.click();
    http.expectOne(r => r.method === 'GET'
      && r.url.endsWith('/datasets/ds-1/versions/v1'))
      .flush({ detail: "dataset version 'ds-1'@'v1' not found" },
        { status: 404, statusText: 'Not Found' });
    await f.whenStable();
    const banner = el.querySelector('.err-banner')?.textContent ?? '';
    expect(banner).toContain('cannot load version');
    expect(banner).toContain('not found');
    expect(el.querySelector('table.cases')).toBeNull();
  });

  it('filters rows by id or name substring', async () => {
    const f = TestBed.createComponent(DatasetsComponent);
    f.detectChanges();
    listReq(http).flush([
      mkDs('ds-a', { name: 'Alpha' }), mkDs('ds-b', { name: 'Beta' }),
      mkDs('other', { name: 'Gamma' }),
    ]);
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(rows(el).length).toBe(3);
    f.componentInstance.search.set('alpha');
    await f.whenStable();
    expect(rows(el).length).toBe(1);
    expect(rows(el)[0].textContent).toContain('ds-a');
  });
});
