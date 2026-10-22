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

// a second trace+replay where playback has somewhere to jump: a tool
// span (kind 'tool' on the span — the entry itself doesn't carry it),
// an event and an error end
const TRACE_TOOL: Trace = {
  trace_id: 't1',
  status: { code: 'error' },
  spans: [
    { span_id: 's1', parent_span_id: null, name: 'root', kind: 'internal',
      start_time: '2026-01-01T00:00:00.000Z',
      end_time: '2026-01-01T00:00:01.000Z',
      status: { code: 'error' }, attributes: {} },
    { span_id: 's2', parent_span_id: 's1', name: 'search', kind: 'tool',
      start_time: '2026-01-01T00:00:00.100Z',
      end_time: '2026-01-01T00:00:00.300Z',
      status: { code: 'error' }, attributes: {} },
  ],
};
const REPLAY_TOOL = {
  trace_id: 't1',
  span_count: 2,
  total_ms: 1000,
  unclosed_spans: 0,
  entries: [
    { t: '2026-01-01T00:00:00+00:00', span_id: 's1', name: 'root',
      kind: 'span_start', offset_ms: 0 },
    { t: '2026-01-01T00:00:00.100+00:00', span_id: 's2', name: 'search',
      kind: 'span_start', offset_ms: 100 },
    { t: '2026-01-01T00:00:00.200+00:00', span_id: 's2', name: 'search',
      kind: 'event', event: 'tool.call', offset_ms: 200 },
    { t: '2026-01-01T00:00:00.300+00:00', span_id: 's2', name: 'search',
      kind: 'span_end', status: 'error', offset_ms: 300 },
    { t: '2026-01-01T00:00:01+00:00', span_id: 's1', name: 'root',
      kind: 'span_end', status: 'error', offset_ms: 1000 },
  ],
};

// mirrors GET /v1/traces/{id}/incident — a clean trace: classification
// only, no hypotheses/suggestions/waste
const INCIDENT_EMPTY = {
  trace_id: 't1', project_id: 'default', session_id: null, status: 'ok',
  failure: { trace_id: 't1', failed: false, category: 'unknown',
             confidence: 'observed', evidence: {}, detail: '' },
  hypotheses: [], eval_suggestions: [],
  graph: { stats: {}, critical_path: { path: [], total_ms: 0, cycles: [] },
           bottlenecks: [] },
  cost: {}, waste: [], replay: { trace_id: 't1', entries: [],
    span_count: 0, total_ms: 0, unclosed_spans: 0 },
  bundle_version: '1.0.0',
  export_sanitize: { redacted_keys: 0, redacted_values: 0,
                     emails_masked: 0, dropped_content: false },
};

// a failed-trace bundle: one supported hypothesis, two candidate eval
// suggestions and one waste finding — the content the CLI prints and
// Studio previously never showed
const INCIDENT = {
  ...INCIDENT_EMPTY,
  status: 'error',
  failure: { trace_id: 't1', failed: true, category: 'tool_error',
             confidence: 'observed', evidence: { span_ids: ['s2'] },
             detail: 'tool span errored' },
  hypotheses: [{
    hypothesis_id: 't1:tool_error', label: 'tool_error',
    symptom: 'primary cause candidate tool_error — 1 span(s)',
    supporting_evidence: ['s2: exception_type:HttpError'],
    contradictory_evidence: [],
    missing_evidence: ['tool input/output not captured'],
    suggested_verification: ['re-run the tool with the captured args'],
    confidence: 'observed', status: 'supported',
  }],
  eval_suggestions: [
    { kind: 'evaluator_spec', name: 'allowed_tools',
      params: { allowed: ['search'] }, params_required: [],
      rationale: 'restricting the allowed surface removes the failure mode',
      evidence: { category: 'tool_error', traces: ['t1'], count: 1,
                  recurring: false, categories: ['tool_error'] },
      classification: 'candidate' },
    { kind: 'evaluator_spec', name: 'tool_success', params: {},
      params_required: [],
      rationale: 'tool_success tracks the failing fraction directly',
      evidence: { category: 'tool_error', traces: ['t1'], count: 1,
                  recurring: false, categories: ['tool_error'] },
      classification: 'candidate' },
  ],
  waste: [{
    kind: 'retry_amplification', span_ids: ['s2'], evidence: {},
    estimated_usd: null, detail: 'same tool+args repeated',
  }],
};

// mirrors GET /v1/traces/{id}/security — one review observation
const SECURITY = {
  trace_id: 't1',
  observations: [{
    kind: 'risky_tool', severity: 'review', span_id: 's2',
    detail: "tool name 'exec' matches the risky-verb watchlist",
  }],
  counts: { risky_tool: 1 }, mode: 'observer',
};
const SECURITY_EMPTY = { trace_id: 't1', observations: [],
                         counts: {}, mode: 'observer' };

// mirrors GET /v1/traces/{id}/efficiency
const EFFICIENCY = {
  trace_id: 't1', outcome: 'error', tool_calls: 3, llm_calls: 2,
  error_spans: 1, retries_observed: 2, input_tokens: 120, output_tokens: 40,
  usage_coverage: '2/2 llm spans', succeeded: false,
};

// mirrors GET /v1/traces/{id}/recommendations
const RECS = {
  trace_id: 't1',
  recommendations: [{
    kind: 'unstable_tool', classification: 'candidate', tool: 'search',
    failures: 2, detail: "tool 'search' failed 2x — flag for retry review",
  }],
  note: 'candidate recommendations — gains require experimental validation',
};
const RECS_EMPTY = { trace_id: 't1', recommendations: [],
  note: 'candidate recommendations — gains require experimental validation' };

// mirrors GET /v1/traces/{id}/correlate
const CORRELATE = {
  trace_id: 't1',
  correlations: [{
    kind: 'tool_failure_cluster', classification: 'temporal_association',
    signals: [
      { type: 'failed_invocations', span_ids: ['s2', 's3'], value: 2 },
      { type: 'all_invocations', span_ids: ['s2', 's3'], value: 2 },
    ],
    strength: { failed_invocations: 2, invocations: 2, failure_share: 1 },
    evidence_span_ids: ['s2', 's3'],
    caveat: 'the cluster is descriptive; a shared cause is unproven',
    detail: "tool 'search' failed 2 of 2 invocations in this trace",
  }],
  signal_inventory: { spans: 2, error_spans: 2 },
  classification: 'temporal_association',
  note: 'correlation is not causation',
};
const CORRELATE_EMPTY = {
  trace_id: 't1', correlations: [], signal_inventory: {},
  classification: 'temporal_association',
  note: 'correlation is not causation',
};

const SPECIALISTS = { specialists: [
  'cost_analyst', 'failure_investigator', 'loop_detection_specialist',
] };

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
  /** The intel pulls fired by ngOnInit — flushed with sane defaults so
   *  tests can reach the panels; override per-test where needed. */
  const flushIntel = (incident: object = INCIDENT_EMPTY) => {
    http.expectOne(r => r.url.endsWith('/traces/t1/incident')).flush(incident);
    http.expectOne(r => r.url.endsWith('/traces/t1/security'))
      .flush(SECURITY_EMPTY);
    http.expectOne(r => r.url.endsWith('/traces/t1/efficiency'))
      .flush(EFFICIENCY);
    http.expectOne(r => r.url.endsWith('/traces/t1/recommendations'))
      .flush(RECS_EMPTY);
    http.expectOne(r => r.url.endsWith('/traces/t1/correlate'))
      .flush(CORRELATE_EMPTY);
    http.expectOne(r => r.url.endsWith('/analyze/specialists'))
      .flush(SPECIALISTS);
  };
  /** Flush the trace fetch + the lazy extras pulls fired by ngOnInit. */
  const flushInit = (replay: object = REPLAY, trace: Trace = TRACE,
                   incident: object = INCIDENT_EMPTY) => {
    http.expectOne(r => r.url.endsWith('/traces/t1')
      && r.method === 'GET').flush(trace);
    http.expectOne(r => r.url.endsWith('/traces/t1/cost')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/failure')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/loops'))
      .flush({ findings: [], classification: 'none' });
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/analysis')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/eval')).flush(EVAL_GET);
    http.expectOne(r => r.url.endsWith('/traces/t1/replay')).flush(replay);
    flushIntel(incident);
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
    flushIntel();
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
    flushIntel();
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
    const btn = [...panel.querySelectorAll('button')]
      .find(b => b.textContent?.includes('show more'))!;
    expect(btn.textContent).toContain('show more (1 hidden)');
    btn.click();
    f.detectChanges();
    expect(panel.querySelectorAll('.replay-table tbody tr').length).toBe(3);
    expect([...panel.querySelectorAll('button')]
      .find(b => b.textContent?.includes('show more'))).toBeUndefined();
  });

  // glyph buttons carry their names in aria-label; text buttons match
  // their text
  const ctlBtn = (panel: Element, label: string) =>
    [...panel.querySelectorAll<HTMLButtonElement>('.replay-controls button')]
      .find(b => b.getAttribute('aria-label') === label ||
        b.textContent?.trim() === label);

  it('step controls walk the entries with a position indicator', () => {
    const f = make();
    flushInit();
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const c = f.componentInstance;
    const panel = replayPanel(f);
    expect(panel.querySelector('.pos')!.textContent).toContain('1/3');
    // parked at the start — back/start disabled, first row highlighted
    expect((ctlBtn(panel, 'step back')! as HTMLButtonElement).disabled)
      .toBe(true);
    expect(panel.querySelector('tr.cur')!.textContent)
      .toContain('span_start');
    ctlBtn(panel, 'step forward')!.click();
    f.detectChanges();
    expect(c.replayPos()).toBe(1);
    expect(panel.querySelector('.pos')!.textContent).toContain('2/3');
    expect(panel.querySelector('tr.cur')!.textContent)
      .toContain('model.call');
    ctlBtn(panel, 'jump to end')!.click();
    f.detectChanges();
    expect(c.replayPos()).toBe(2);
    expect((ctlBtn(panel, 'step forward')! as HTMLButtonElement).disabled)
      .toBe(true);
    ctlBtn(panel, 'jump to start')!.click();
    f.detectChanges();
    expect(c.replayPos()).toBe(0);
  });

  it('arrow keys step and space toggles play while the panel has focus', () => {
    const f = make();
    flushInit();
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const c = f.componentInstance;
    const panel = replayPanel(f);
    const box = panel.querySelector('.replay-controls') as HTMLElement;
    box.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'ArrowRight', bubbles: true }));
    expect(c.replayPos()).toBe(1);
    box.dispatchEvent(new KeyboardEvent('keydown',
      { key: 'ArrowLeft', bubbles: true }));
    expect(c.replayPos()).toBe(0);
    box.dispatchEvent(new KeyboardEvent('keydown',
      { key: ' ', bubbles: true }));
    expect(c.playing()).toBe(true);
    box.dispatchEvent(new KeyboardEvent('keydown',
      { key: ' ', bubbles: true }));
    expect(c.playing()).toBe(false);
    // a key elsewhere does nothing — the handler lives on the panel
    (f.nativeElement as HTMLElement).dispatchEvent(
      new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
    expect(c.replayPos()).toBe(0);
  });

  it('play auto-advances on the speed-scaled interval and stops at the end', () => {
    vi.useFakeTimers();
    try {
      const f = make();
      flushInit();
      const c = f.componentInstance;
      c.tab.set('evidence');
      c.playReplay();
      expect(c.playing()).toBe(true);
      vi.advanceTimersByTime(2100);      // 1× → 1000ms/step
      expect(c.replayPos()).toBe(2);     // clamped at last entry…
      expect(c.playing()).toBe(true);
      vi.advanceTimersByTime(1000);      // …then auto-pauses at the end
      expect(c.playing()).toBe(false);
      expect(c.replayPos()).toBe(2);
      c.pauseReplay();
    } finally {
      vi.useRealTimers();
    }
  });

  it('a faster speed shortens the auto-advance interval; manual nav pauses', () => {
    vi.useFakeTimers();
    try {
      const f = make();
      flushInit();
      const c = f.componentInstance;
      c.setSpeed(4);                      // 4× → 250ms/step
      c.playReplay();
      vi.advanceTimersByTime(500);
      expect(c.replayPos()).toBe(2);     // clamped to the last of 3
      c.replayStart();                    // manual nav pauses playback
      expect(c.playing()).toBe(false);
      vi.advanceTimersByTime(2000);
      expect(c.replayPos()).toBe(0);     // nothing advanced while paused
    } finally {
      vi.useRealTimers();
    }
  });

  it('jump buttons land on the next matching entry, wrapping around', () => {
    const f = make();
    flushInit(REPLAY_TOOL, TRACE_TOOL);
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const c = f.componentInstance;
    const panel = replayPanel(f);
    // all three jump targets exist in this timeline
    expect((ctlBtn(panel, 'next error')! as HTMLButtonElement).disabled)
      .toBe(false);
    expect((ctlBtn(panel, 'next tool')! as HTMLButtonElement).disabled)
      .toBe(false);
    ctlBtn(panel, 'next error')!.click();
    expect(c.replayPos()).toBe(3);       // s2 span_end status error
    ctlBtn(panel, 'next error')!.click();
    expect(c.replayPos()).toBe(4);       // s1 span_end status error
    ctlBtn(panel, 'next tool')!.click();
    expect(c.replayPos()).toBe(1);       // wraps to s2's span_start
    ctlBtn(panel, 'next event')!.click();
    expect(c.replayPos()).toBe(2);       // the tool.call event
    f.detectChanges();
    expect(panel.querySelector('tr.cur')!.textContent)
      .toContain('tool.call');
  });

  it('a jump target that does not exist disables its button', () => {
    const f = make();
    flushInit();                          // no error / no tool span
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = replayPanel(f);
    expect((ctlBtn(panel, 'next error')! as HTMLButtonElement).disabled)
      .toBe(true);
    expect((ctlBtn(panel, 'next tool')! as HTMLButtonElement).disabled)
      .toBe(true);
    expect((ctlBtn(panel, 'next event')! as HTMLButtonElement).disabled)
      .toBe(false);
  });

  it('stepping beyond the rendered cap expands it', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    c.tab.set('evidence');
    c.replayShown.set(2);
    f.detectChanges();
    const panel = replayPanel(f);
    expect(panel.querySelectorAll('.replay-table tbody tr').length).toBe(2);
    ctlBtn(panel, 'step forward')!.click();
    ctlBtn(panel, 'step forward')!.click();
    f.detectChanges();
    expect(c.replayPos()).toBe(2);
    expect(c.replayShown()).toBe(3);
    expect(panel.querySelectorAll('.replay-table tbody tr').length).toBe(3);
    expect(panel.querySelector('tr.cur')!.textContent).toContain('span_end');
  });

  // -- incident + intelligence panels ---------------------------------

  const incidentPanel = (f: { nativeElement: HTMLElement }) =>
    f.nativeElement.querySelector('details.incident')!;

  it('incident renders eval_suggestions, hypotheses and waste', () => {
    const f = make();
    flushInit(REPLAY, TRACE, INCIDENT);
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = incidentPanel(f);
    // summary counts the evidence buckets
    expect(panel.querySelector('summary')!.textContent)
      .toContain('1 hypotheses');
    expect(panel.querySelector('summary')!.textContent)
      .toContain('2 eval suggestion(s)');
    expect(panel.querySelector('summary')!.textContent)
      .toContain('1 waste');
    // classification line
    expect(panel.textContent).toContain('tool_error');
    expect(panel.textContent).toContain('tool span errored');
    // hypothesis ledger — status/confidence plus the evidence lists
    expect(panel.textContent).toContain('supported');
    expect(panel.textContent).toContain('s2: exception_type:HttpError');
    expect(panel.textContent).toContain('tool input/output not captured');
    // the CLI-visible suggestions the audit found missing in Studio
    const rows = panel.querySelectorAll('.eval-sugg tbody tr');
    expect(rows.length).toBe(2);
    const sugg = panel.querySelector('.eval-sugg')!.textContent!;
    expect(sugg).toContain('allowed_tools');
    expect(sugg).toContain('tool_success');
    expect(sugg).toContain('evaluator_spec');
    expect(sugg).toContain('tool_error · 1 trace(s)');
    // waste row
    const waste = panel.querySelector('.waste')!.textContent!;
    expect(waste).toContain('retry_amplification');
    expect(waste).toContain('s2');
    expect(waste).toContain('—');   // estimated_usd null → honest dash
  });

  it('a clean incident bundle says so instead of empty tables', () => {
    const f = make();
    flushInit();                 // INCIDENT_EMPTY
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = incidentPanel(f);
    expect(panel.querySelector('summary')!.textContent).toContain('clean');
    expect(panel.querySelector('.eval-sugg')).toBeNull();
    expect(panel.textContent).toContain('no hypotheses');
  });

  it('a failed incident pull shows unavailable, never a crash', () => {
    const f = make();
    http.expectOne(r => r.url.endsWith('/traces/t1')
      && r.method === 'GET').flush(TRACE);
    http.expectOne(r => r.url.endsWith('/traces/t1/cost')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/failure')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/loops'))
      .flush({ findings: [], classification: 'none' });
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/analysis')).flush({});
    http.expectOne(r => r.url.endsWith('/traces/t1/eval')).flush(EVAL_GET);
    http.expectOne(r => r.url.endsWith('/traces/t1/replay')).flush(REPLAY);
    http.expectOne(r => r.url.endsWith('/traces/t1/incident'))
      .flush('gone', { status: 404, statusText: 'Not Found' });
    http.expectOne(r => r.url.endsWith('/traces/t1/security'))
      .flush(SECURITY_EMPTY);
    http.expectOne(r => r.url.endsWith('/traces/t1/efficiency'))
      .flush(EFFICIENCY);
    http.expectOne(r => r.url.endsWith('/traces/t1/recommendations'))
      .flush(RECS_EMPTY);
    http.expectOne(r => r.url.endsWith('/traces/t1/correlate'))
      .flush(CORRELATE_EMPTY);
    http.expectOne(r => r.url.endsWith('/analyze/specialists'))
      .flush(SPECIALISTS);
    f.componentInstance.tab.set('evidence');
    f.detectChanges();
    const panel = incidentPanel(f);
    expect(panel.textContent).toContain('incident bundle unavailable');
    expect(panel.querySelector('.eval-sugg')).toBeNull();
  });

  it('security/efficiency/recommendations/correlate panels render rows', () => {
    const f = make();
    flushInit();
    // the intel pulls flushed by flushInit used empties — push real data
    // directly through the extras signal to render the row paths
    const c = f.componentInstance;
    c.extras.update(e => ({ ...e, security: SECURITY,
                             recommendations: RECS, correlate: CORRELATE }));
    c.tab.set('evidence');
    f.detectChanges();
    const el = f.nativeElement as HTMLElement;
    const sec = el.querySelector('details.intel-security')!;
    expect(sec.textContent).toContain('1 observation(s)');
    expect(sec.textContent).toContain('risky_tool');
    expect(sec.textContent).toContain('exec');
    const eff = el.querySelector('details.intel-efficiency')!;
    expect(eff.textContent).toContain('2 llm · 3 tool');
    expect(eff.textContent).toContain('120 in · 40 out');
    expect(eff.textContent).toContain('2/2 llm spans');
    const rec = el.querySelector('details.intel-recs')!;
    expect(rec.textContent).toContain('unstable_tool');
    expect(rec.textContent).toContain('search ×2');
    const cor = el.querySelector('details.intel-correlate')!;
    expect(cor.textContent).toContain('tool_failure_cluster');
    expect(cor.textContent).toContain('shared cause is unproven');
    expect(cor.textContent).toContain('s2, s3');
  });

  it('analyze posts /v1/analyze/{specialist}/{id} with {} body', () => {
    const f = make();
    flushInit();
    const c = f.componentInstance;
    c.tab.set('evidence');
    c.specialistSel = 'failure_investigator';
    c.runAnalysis();
    const req = http.expectOne(r =>
      r.url.endsWith('/analyze/failure_investigator/t1'));
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({});
    req.flush({
      run_id: 'failure_investigator:t1', specialist: 'failure_investigator',
      stop_reason: 'completed', iterations: 1, tokens_observed: 0,
      cancelled: false,
      findings: [{ failed: true, category: 'tool_error' }],
    });
    f.detectChanges();
    const panel = (f.nativeElement as HTMLElement)
      .querySelector('details.analyze')!;
    expect(panel.textContent).toContain('completed');
    expect(panel.textContent).toContain('tool_error');
    expect(c.analysisRunning()).toBe(false);
  });
});
