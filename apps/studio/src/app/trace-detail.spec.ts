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

// mirrors GET /v1/traces/{id}/replay — entries pre-ordered server-side
// by (timestamp, span_id, kind): start < event < end at equal t
const REPLAY = {
  trace_id: 't1',
  span_count: 1,
  total_ms: 1000,
  unclosed_spans: 0,
  entries: [
    { t: '2026-01-01T00:00:00+00:00', span_id: 's1', name: 'root',
      kind: 'span_start', offset_ms: 0 },
    { t: '2026-01-01T00:00:00.500+00:00', span_id: 's1', name: 'root',
      kind: 'event', event: 'model.call', offset_ms: 500 },
    { t: '2026-01-01T00:00:01+00:00', span_id: 's1', name: 'root',
      kind: 'span_end', status: 'ok', offset_ms: 1000 },
  ],
};

describe('TraceDetailComponent', () => {
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
  const flushInit = (replay: object = REPLAY) => {
    http.expectOne(r => r.url.endsWith('/traces/t1')
      && r.method === 'GET').flush(TRACE);
    http.expectOne(r => r.url.endsWith('/traces/t1/cost')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/failure')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/loops'))
      .flush({ findings: [], classification: 'none' });
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/analysis')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/eval')).flush(EVAL_GET);
    http.expectOne(r => r.url.endsWith('/traces/t1/replay')).flush(replay);
  };
  const evalPanel = (f: { nativeElement: HTMLElement }) =>
    f.nativeElement.querySelector('.panel.eval')!;
  const replayPanel = (f: { nativeElement: HTMLElement }) =>
    f.nativeElement.querySelector('details.replay')!;

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
    http.expectOne(r => r.url.endsWith('/traces/t1/replay')).flush(REPLAY);
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = evalPanel(f);
    expect(panel.querySelector('.eval-table')).toBeNull();
    expect(panel.textContent).toContain('eval unavailable');
  });

  it('typed params serialize into the POST body per kind', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    c.evalSel.set(new Set([
      'allowed_tools', 'contains', 'exact_match',
      'latency_budget', 'task_completion',
    ]));
    c.setEvalParam('allowed_tools', 'allowed', 'search, read');
    c.setEvalParam('contains', 'text', 'hello');
    c.setEvalParam('contains', 'case_sensitive', 'false');
    c.setEvalParam('exact_match', 'field', 'output.value');
    c.setEvalParam('exact_match', 'expected', '42');   // json kind → number
    c.setEvalParam('latency_budget', 'max_duration_ms', '5000');
    c.setEvalParam('task_completion', 'require_no_error', 'false');
    c.tab.set('evidence');
    f.detectChanges();
    c.runEval();
    const req = http.expectOne(r => r.url.endsWith('/eval/run'));
    // sorted evaluator order; every kind parsed to its wire type
    expect(req.request.body).toEqual({
      trace_ids: ['t1'],
      evaluators: [
        { name: 'allowed_tools', params: { allowed: ['search', 'read'] } },
        { name: 'contains',
          params: { text: 'hello', case_sensitive: false } },
        { name: 'exact_match',
          params: { field: 'output.value', expected: 42 } },
        { name: 'latency_budget', params: { max_duration_ms: 5000 } },
        { name: 'task_completion', params: { require_no_error: false } },
      ],
    });
    req.flush({ dataset: null, trace_ids: ['t1'], summary: {},
                results: { t1: [] } });
  });

  it('a malformed param value blocks the run before POST', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    c.evalSel.set(new Set(['latency_budget']));
    c.setEvalParam('latency_budget', 'max_duration_ms', 'soon');
    c.tab.set('evidence');
    f.detectChanges();
    c.runEval();
    http.expectNone(r => r.url.endsWith('/eval/run'));
    f.detectChanges();
    const banner = evalPanel(f).querySelector('.err-banner')!.textContent!;
    expect(banner).toContain('latency_budget');
    expect(banner).toContain('max_duration_ms');
    expect(c.evalRunning()).toBe(false);
  });

  it('a fractional tool_call_count bound is malformed (int ≥ 0)', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    c.evalSel.set(new Set(['tool_call_count']));
    c.setEvalParam('tool_call_count', 'min', '1.5');
    c.tab.set('evidence');
    f.detectChanges();
    c.runEval();
    http.expectNone(r => r.url.endsWith('/eval/run'));
    expect(c.evalErr()).toContain('min');
  });

  it('checking a param evaluator renders inputs; typing feeds state', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    c.tab.set('evidence');
    f.detectChanges();
    // default selection (error_free, tool_success) takes no params
    expect(evalPanel(f).querySelector('.eval-params')).toBeNull();
    c.evalSel.set(new Set(['regex']));
    f.detectChanges();
    const row = evalPanel(f).querySelector('.eval-params')!;
    expect(row.textContent).toContain('regex');
    expect(row.textContent).toContain('pattern*');
    const inputs = row.querySelectorAll('input');
    expect(inputs.length).toBe(2);            // pattern + field
    expect(row.querySelectorAll('select').length).toBe(1);  // case_sensitive
    const inp = inputs[0] as HTMLInputElement;
    inp.value = 'foo.*';
    inp.dispatchEvent(new Event('input'));
    expect(c.evalParams()['regex']!['pattern']).toBe('foo.*');
    // unchecking hides the row; the typed value survives a re-check
    c.toggleEval('regex');
    f.detectChanges();
    expect(evalPanel(f).querySelector('.eval-params')).toBeNull();
    c.toggleEval('regex');
    f.detectChanges();
    expect((evalPanel(f).querySelector('.eval-params input') as HTMLInputElement)
      .value).toBe('foo.*');
  });

  it('renders the replay timeline in the server order', () => {
    const f = make();
    flushInit();
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = replayPanel(f);
    const rows = panel.querySelectorAll('.replay-table tbody tr');
    expect(rows.length).toBe(3);
    // ordered span_start → event → span_end, offsets ascending
    expect(rows[0].textContent).toContain('span_start');
    expect(rows[0].textContent).toContain('root');
    expect(rows[1].textContent).toContain('event');
    expect(rows[1].textContent).toContain('model.call');
    expect(rows[1].textContent).toContain('500');
    expect(rows[2].textContent).toContain('span_end');
    expect(rows[2].textContent).toContain('ok');
    expect(rows[2].textContent).toContain('s1');
    // collapsed summary still carries the totals
    expect(panel.querySelector('summary')!.textContent)
      .toContain('3 events · 1000ms');
    // the panel issues no writes
    http.expectNone(r => r.method === 'POST');
  });

  it('an empty replay renders an honest empty state', () => {
    const f = make();
    flushInit({ trace_id: 't1', entries: [], span_count: 0,
                total_ms: 0, unclosed_spans: 0 });
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = replayPanel(f);
    expect(panel.querySelector('.replay-table')).toBeNull();
    expect(panel.textContent).toContain('no recorded timeline');
  });

  it('a failed replay fetch surfaces the error banner', () => {
    const f = TestBed.createComponent(TraceDetailComponent);
    f.detectChanges();
    http.expectOne(r => r.url.endsWith('/traces/t1')
      && r.method === 'GET').flush(TRACE);
    http.expectOne(r => r.url.endsWith('/traces/t1/cost')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/failure')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/loops'))
      .flush({ findings: [], classification: 'none' });
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/analysis')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/eval')).flush(EVAL_GET);
    http.expectOne(r => r.url.endsWith('/traces/t1/replay'))
      .flush('down', { status: 500, statusText: 'Server Error' });
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const banner = replayPanel(f).querySelector('.err-banner')!.textContent!;
    expect(banner).toContain('replay unavailable');
    expect(banner).toContain('500');
    expect(replayPanel(f).querySelector('.replay-table')).toBeNull();
  });

  it('caps the timeline behind a show-more button', () => {
    const f = make();
    flushInit();
    f.componentInstance.tab.set('evidence');
    f.componentInstance.replayShown.set(2);
    f.detectChanges();
    const panel = replayPanel(f);
    expect(panel.querySelectorAll('.replay-table tbody tr').length).toBe(2);
    const btn = panel.querySelector('button')!;
    expect(btn.textContent).toContain('show more (1 hidden)');
    btn.click();
    f.detectChanges();
    expect(panel.querySelectorAll('.replay-table tbody tr').length).toBe(3);
    expect(panel.querySelector('button')).toBeNull();
  });
});
