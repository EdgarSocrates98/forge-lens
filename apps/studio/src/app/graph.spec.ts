import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, ParamMap, Router, convertToParamMap,
  provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting }
  from '@angular/common/http/testing';
import { GraphComponent } from './graph';
import { Bottleneck, CriticalPath, ExecutionGraph, GraphNode }
  from './api';

const node = (id: string, over: Partial<GraphNode> = {}): GraphNode => ({
  id, kind: 'internal', name: `span ${id}`,
  start_time: '2026-01-01T00:00:00.000Z',
  end_time: '2026-01-01T00:00:01.000Z',
  duration_ms: 1000, status: 'ok',
  integrity_flags: [], has_token_usage: false, error: false,
  ...over,
});

// a root with a parent→llm and parent→tool child plus a sibling
// sequence edge — mirrors build_trace_graph's shape
const GRAPH: ExecutionGraph = {
  subject_id: 't1', project_id: 'p1', root_id: 's1',
  nodes: [
    node('s1', { kind: 'agent', name: 'agent root', duration_ms: 1400 }),
    node('s2', { kind: 'llm', name: 'llm call', duration_ms: 400 }),
    node('s3', { kind: 'tool', name: 'tool x', duration_ms: 300,
      status: 'error', error: true }),
  ],
  edges: [
    { source: 's1', target: 's2', kind: 'parent' },
    { source: 's1', target: 's3', kind: 'parent' },
    { source: 's2', target: 's3', kind: 'sequence' },
  ],
  stats: { span_count: 3, edge_count: 3, max_depth: 2,
    dangling_edges: 0, missing_parents: 0, cycle_members: 0 },
};

const CP: CriticalPath = { path: ['s1', 's2'], total_ms: 1800, cycles: [] };
const BN: { items: Bottleneck[] } = {
  items: [
    { id: 's1', name: 'agent root', kind: 'agent',
      self_ms: 700, duration_ms: 1400 },
    { id: 's3', name: 'tool x', kind: 'tool',
      self_ms: 300, duration_ms: 300 },
  ],
};

const big = (n: number): ExecutionGraph => ({
  subject_id: 'tbig', project_id: 'p1', root_id: 'n0',
  nodes: Array.from({ length: n }, (_, i) => node(`n${i}`)),
  edges: [],
  stats: { span_count: n, edge_count: 0 },
});

let params: ParamMap = convertToParamMap({});

describe('GraphComponent', () => {
  let http: HttpTestingController;

  beforeEach(async () => {
    localStorage.clear();
    params = convertToParamMap({});
    await TestBed.configureTestingModule({
      imports: [GraphComponent],
      providers: [
        provideRouter([{ path: '**', component: GraphComponent }]),
        provideHttpClient(),
        provideHttpClientTesting(),
        // the component only reads snapshot.queryParamMap — stub it
        // directly so deep-link state can be set without a navigation
        { provide: ActivatedRoute,
          useFactory: () => ({ snapshot: { queryParamMap: params } }) },
      ],
    }).compileComponents();
    http = TestBed.inject(HttpTestingController);
    vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  });
  afterEach(() => http.verify());

  const make = () => {
    const f = TestBed.createComponent(GraphComponent);
    f.detectChanges();
    return f;
  };
  /** Flush the trace-graph load + the bottlenecks pull it triggers. */
  const flushTrace = (g: ExecutionGraph = GRAPH) => {
    http.expectOne(r => r.url.endsWith('/graph/traces/t1')
      && r.method === 'GET').flush(g);
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/bottlenecks'))
      .flush(BN);
  };

  it('renders the fixture graph as layered SVG nodes and edges', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace();
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const nodes = el.querySelectorAll('svg .node');
    expect(nodes.length).toBe(3);
    const edges = el.querySelectorAll('svg line.edge');
    expect(edges.length).toBe(3);
    expect(el.querySelectorAll('svg line.edge.seq').length).toBe(1);
    // error node is coloured like the waterfall's error bars
    expect(el.querySelectorAll('svg .node.err').length).toBe(1);
    expect(el.textContent).toContain('agent root');
    // bottlenecks panel loaded alongside
    expect(el.textContent).toContain('bottlenecks');
    expect(el.querySelectorAll('aside tbody tr').length).toBe(2);
  });

  it('highlights the critical-path node ids on toggle', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace();
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const btn = [...el.querySelectorAll('button')]
      .find(b => b.textContent?.includes('critical path'))!;
    btn.click();
    http.expectOne(r => r.url.endsWith('/graph/traces/t1/critical-path'))
      .flush(CP);
    await f.whenStable();
    expect(el.querySelectorAll('svg .node.cp').length).toBe(2);
    expect(el.textContent).toContain('1800ms');
    btn.click(); // toggle off — cached, no refetch
    await f.whenStable();
    expect(el.querySelectorAll('svg .node.cp').length).toBe(0);
  });

  it('shows node details on click with a link to the trace', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace();
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    const g = [...el.querySelectorAll('svg .node')]
      .find(n => n.textContent?.includes('tool x'))!;
    (g.querySelector('rect') as SVGRectElement)
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await f.whenStable();
    const panel = el.querySelector('aside .panel')!;
    expect(panel.textContent).toContain('tool x');
    expect(panel.textContent).toContain('s3');
    expect(panel.textContent).toContain('status error');
    const a = panel.querySelector('a');
    expect(a?.getAttribute('href')).toContain('/traces/t1');
  });

  it('caps rendering at 500 nodes with a truncation banner', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace(big(600));
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelectorAll('svg .node').length).toBe(500);
    expect(el.querySelector('.err-banner')?.textContent)
      .toContain('truncated');
    expect(el.querySelector('.err-banner')?.textContent).toContain('600');
  });

  it('shows an error banner on 404', async () => {
    params = convertToParamMap({ trace_id: 'nope' });
    const f = make();
    http.expectOne(r => r.url.endsWith('/graph/traces/nope'))
      .flush('trace not found', { status: 404, statusText: 'Not Found' });
    http.expectOne(r => r.url.endsWith('/graph/traces/nope/bottlenecks'))
      .flush('trace not found', { status: 404, statusText: 'Not Found' });
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelector('.err-banner')?.textContent).toContain('404');
    expect(el.querySelectorAll('svg .node').length).toBe(0);
  });

  it('loads a session graph when ?session_id= is deep-linked', async () => {
    params = convertToParamMap({ session_id: 'sess-1' });
    const f = make();
    http.expectOne(r => r.url.endsWith('/graph/sessions/sess-1')).flush({
      subject_id: 'sess-1', project_id: 'p1', root_id: 'ta',
      nodes: [
        node('ta', { kind: 'trace', name: 'trace a' }),
        node('tb', { kind: 'trace', name: 'trace b', status: 'error',
          error: true }),
      ],
      edges: [{ source: 'ta', target: 'tb', kind: 'sequence' }],
      stats: { trace_count: 2, edge_count: 1, dangling_edges: 0,
        truncated: false },
    });
    await f.whenStable();
    const el = f.nativeElement as HTMLElement;
    expect(el.querySelectorAll('svg .node').length).toBe(2);
    expect(el.querySelectorAll('svg line.edge.seq').length).toBe(1);
    // session mode: no critical-path/bottlenecks (trace-scoped routes)
    expect(el.textContent).not.toContain('critical path');
    // a session node links to its own trace detail
    const g = [...el.querySelectorAll('svg .node')]
      .find(n => n.textContent?.includes('trace b'))!;
    (g.querySelector('rect') as SVGRectElement)
      .dispatchEvent(new MouseEvent('click', { bubbles: true }));
    await f.whenStable();
    const a = el.querySelector('aside .panel a');
    expect(a?.getAttribute('href')).toContain('/traces/tb');
  });
});
