import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter }
  from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { TraceDetailComponent } from './trace-detail';
import { Trace } from './api';

const TRACE: Trace = {
  trace_id: 't1',
  status: { code: 'ok' },
  spans: [{
    span_id: 's1', parent_span_id: null, name: 'root', kind: 'internal',
    start_time: '2026-01-01T00:00:00.000Z',
    end_time: '2026-01-01T00:00:01.000Z',
    status: { code: 'ok' }, attributes: {},
  }],
};

// mirrors GET /v1/traces/{id}/eval — one scored row, one `score: null`
// whose only evidence is details.reason
const EVAL_GET = {
  trace_id: 't1',
  results: [
    { evaluator: 'error_free', metric: 'no_error_spans', score: 1.0,
      details: {} },
    { evaluator: 'tool_success', metric: 'tool_success_rate', score: null,
      details: { reason: 'no tool spans in trace' } },
  ],
};

describe('TraceDetailComponent eval panel', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    localStorage.clear();
    await TestBed.configureTestingModule({
      imports: [TraceDetailComponent],
      providers: [
        provideRouter([{ path: '**', component: TraceDetailComponent }]),
        provideHttpClient(),
        provideHttpClientTesting(),
        // the component only reads snapshot.paramMap — stub the id directly
        { provide: ActivatedRoute,
          useFactory: () => ({
            snapshot: { paramMap: convertToParamMap({ id: 't1' }) } }) },
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
  });
  afterEach(() => http.verify());

  const make = () => {
    const f = TestBed.createComponent(TraceDetailComponent);
    f.detectChanges();
    return f;
  };
  /** Flush the trace fetch + the lazy extras pulls fired by ngOnInit. */
  const flushInit = () => {
    http.expectOne(r => r.url.endsWith('/traces/t1')
      && r.method === 'GET').flush(TRACE);
    http.expectOne(r => r.url.endsWith('/traces/t1/cost')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/failure')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/loops'))
      .flush({ findings: [], classification: 'none' });
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/analysis')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/eval')).flush(EVAL_GET);
  };
  const evalPanel = (f: { nativeElement: HTMLElement }) =>
    f.nativeElement.querySelector('.panel.eval')!;

  it('renders the GET eval battery; a null score shows n/a + reason', () => {
    const f = make();
    flushInit();
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = evalPanel(f);
    const rows = panel.querySelectorAll('.eval-table tbody tr');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('error_free');
    expect(rows[0].textContent).toContain('1');
    expect(rows[1].textContent).toContain('tool_success');
    expect(rows[1].textContent).toContain('n/a');
    expect(rows[1].textContent).toContain('no tool spans in trace');
  });

  it('run posts the selected evaluators as specs and renders the scores', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    c.evalSel.set(new Set(['error_free', 'contains']));
    c.tab.set('evidence');
    f.detectChanges();
    c.runEval();
    const req = http.expectOne(r => r.url.endsWith('/eval/run'));
    expect(req.request.method).toBe('POST');
    // wire shape: trace_ids (list) + {name} spec objects, sorted
    expect(req.request.body).toEqual({
      trace_ids: ['t1'],
      evaluators: [{ name: 'contains' }, { name: 'error_free' }],
    });
    req.flush({
      dataset: null, trace_ids: ['t1'], summary: {},
      results: { t1: [
        { evaluator: 'contains', metric: 'output_contains', score: null,
          details: { reason: 'field not observed as string' } },
        { evaluator: 'error_free', metric: 'no_error_spans', score: 1.0,
          details: {} },
      ] },
    });
    f.detectChanges();
    const panel = evalPanel(f);
    const rows = panel.querySelectorAll('.eval-table tbody tr');
    expect(rows.length).toBe(2);
    // run results override the GET battery rows (the checkbox list still
    // names tool_success, so scope assertions to the table)
    const table = panel.querySelector('.eval-table')!.textContent!;
    expect(table).toContain('contains');
    expect(table).toContain('field not observed as string');
    expect(table).not.toContain('tool_success');
    expect(c.evalRunning()).toBe(false);
  });

  it('checkboxes drive the selection set', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    // GET battery evaluators are pre-checked
    expect([...c.evalSel()].sort()).toEqual(['error_free', 'tool_success']);
    c.toggleEval('regex');
    expect(c.evalSel().has('regex')).toBe(true);
    c.toggleEval('error_free');
    expect(c.evalSel().has('error_free')).toBe(false);
    c.toggleEval('regex');
    expect(c.evalSel().has('regex')).toBe(false);
  });

  it('a failed run surfaces the error banner', () => {
    const f = make();
    flushInit();
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    f.componentInstance.runEval();
    http.expectOne(r => r.url.endsWith('/eval/run'))
      .flush('boom', { status: 500, statusText: 'Server Error' });
    f.detectChanges();
    expect(evalPanel(f).querySelector('.err-banner')?.textContent)
      .toContain('eval run failed');
    expect(evalPanel(f).querySelector('.err-banner')?.textContent)
      .toContain('500');
  });

  it('a failed GET battery shows "eval unavailable", not a verdict', () => {
    const f = TestBed.createComponent(TraceDetailComponent);
    f.detectChanges();
    http.expectOne(r => r.url.endsWith('/traces/t1')
      && r.method === 'GET').flush(TRACE);
    http.expectOne(r => r.url.endsWith('/traces/t1/cost')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/failure')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/loops'))
      .flush({ findings: [], classification: 'none' });
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/analysis')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/eval'))
      .flush('down', { status: 500, statusText: 'Server Error' });
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = evalPanel(f);
    expect(panel.querySelector('.eval-table')).toBeNull();
    expect(panel.textContent).toContain('eval unavailable');
  });
});
