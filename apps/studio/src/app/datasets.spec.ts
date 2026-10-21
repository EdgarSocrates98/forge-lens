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
    const text = el.textContent ?? '';
    expect(text).toContain('insufficient_data');
    expect(text).toContain('n/a');           // p_value: null, honest
    expect(text).toContain('failed');        // gate verdict
    expect(text).toContain('statistically significant regression');
    expect(text).toContain('ds-1@v1');
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
