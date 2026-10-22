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

  /** Chain n0→n1→…— under the cap the drawn set must be a connected
   *  prefix: no drawn node may have a drawn-out parent. */
  const chain = (n: number): ExecutionGraph => ({
    subject_id: 'tchain', project_id: 'p1', root_id: 'n0',
    nodes: Array.from({ length: n }, (_, i) => node(`n${i}`)),
    edges: Array.from({ length: n - 1 }, (_, i) =>
      ({ source: `n${i}`, target: `n${i + 1}`, kind: 'parent' })),
    stats: { span_count: n, edge_count: n - 1 },
  });

  it('draws a parent-closed BFS prefix when the cap cuts the graph', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace(chain(600));
    await f.whenStable();
    const c = f.componentInstance;
    const drawnIds = new Set(c.drawn().map(n => n.id));
    expect(drawnIds.size).toBe(500);
    expect(drawnIds.has('n0')).toBe(true);
    expect(drawnIds.has('n499')).toBe(true);
    expect(drawnIds.has('n599')).toBe(false);
    // parent-closed: every drawn node's parent edge stays inside the set
    for (const e of chain(600).edges) {
      if (drawnIds.has(e.target)) expect(drawnIds.has(e.source)).toBe(true);
    }
    const el = f.nativeElement as HTMLElement;
    expect(el.textContent).toContain('drawn 500 of 600');
  });

  it('find centers the camera on a match and marks hits', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace(chain(600));
    await f.whenStable();
    const c = f.componentInstance;
    c.onFindInput('n42');
    await f.whenStable();
    expect(c.matches().length).toBeGreaterThan(0);
    c.find();
    await f.whenStable();
    expect(c.sel()?.id).toBe('n42');
    // camera zoomed to the readable window and contains the node
    const el = f.nativeElement as HTMLElement;
    const g = [...el.querySelectorAll('svg .node')]
      .find(n => n.querySelector('title')?.textContent?.includes('(n42)'))!;
    const rect = g.querySelector('rect')!;
    const nx = Number(rect.getAttribute('x'));
    const v = c.vb();
    expect(nx).toBeGreaterThanOrEqual(v.x);
    expect(nx).toBeLessThanOrEqual(v.x + v.w);
    expect(v.w).toBeLessThanOrEqual(190 * 8 + 1);
    // hit marks apply to every match that survived cap AND viewport cull
    const visible = new Set(c.vlnodes().map(l => l.n.id));
    const hitCount = c.matches().filter(id => visible.has(id)).length;
    expect(hitCount).toBeGreaterThan(0);
    expect(el.querySelectorAll('svg .node.hit').length).toBe(hitCount);
  });

  it('find reveals a node beyond the cap instead of losing it', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace(chain(600));
    await f.whenStable();
    const c = f.componentInstance;
    expect(c.drawn().some(n => n.id === 'n599')).toBe(false);
    c.onFindInput('n599');
    c.find();
    await f.whenStable();
    expect(c.sel()?.id).toBe('n599');
    expect(c.drawn().some(n => n.id === 'n599')).toBe(true);
    // the cap still holds — n599 displaced one BFS-tail node
    expect(c.drawn().length).toBe(500);
    const el = f.nativeElement as HTMLElement;
    expect(el.textContent).toContain('n599');
  });

  it('lists in/out neighbors, traverses on click and expands the cap', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace(chain(600));
    await f.whenStable();
    const c = f.componentInstance;
    c.go('n250'); // drawn; its child n251 is also drawn (prefix)
    await f.whenStable();
    let el = f.nativeElement as HTMLElement;
    expect(el.textContent).toContain('1 in-edge(s)');
    expect(el.textContent).toContain('1 out-edge(s)');
    // traverse via the out-edge button — the last .nb lists the
    // out-edge neighbor (n251) after the in-edge one (n249)
    const outBtns = [...el.querySelectorAll('button.nb')] as HTMLElement[];
    outBtns[outBtns.length - 1].click();
    await f.whenStable();
    expect(c.sel()?.id).toBe('n251');
    // now select the cap-edge node: n499's child n500 is beyond the cap
    c.go('n499');
    await f.whenStable();
    el = f.nativeElement as HTMLElement;
    expect(c.hiddenNeighbors()).toBe(1);
    const expand = [...el.querySelectorAll('button')]
      .find(b => b.textContent?.includes('expand neighbors'))!;
    expand.click();
    await f.whenStable();
    expect(c.hiddenNeighbors()).toBe(0);
    expect(c.drawn().some(n => n.id === 'n500')).toBe(true);
    expect(c.drawn().length).toBe(500);
  });

  it('culls nodes outside the viewport as the camera zooms in', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace(chain(600));
    await f.whenStable();
    const c = f.componentInstance;
    const el = f.nativeElement as HTMLElement;
    // fit view: all 500 drawn nodes are inside the viewBox
    expect(el.querySelectorAll('svg .node').length).toBe(500);
    c.centerOn('n250'); // readable-width window around one node
    await f.whenStable();
    const shown = el.querySelectorAll('svg .node').length;
    expect(shown).toBeLessThan(500);
    expect(shown).toBeGreaterThan(0);
    expect(c.vlnodes().length).toBe(shown);
    // the culled node is not in the DOM, the centered one is
    const titles = [...el.querySelectorAll('svg .node title')]
      .map(t => t.textContent ?? '');
    expect(titles.some(t => t.includes('(n250)'))).toBe(true);
    expect(titles.some(t => t.includes('(n499)'))).toBe(false);
  });

  it('clamps panning so the viewport cannot escape the content', async () => {
    params = convertToParamMap({ trace_id: 't1' });
    const f = make();
    flushTrace(chain(10));
    await f.whenStable();
    const c = f.componentInstance;
    const svg = {
      getBoundingClientRect: () => ({ width: 1000, height: 600 }),
    } as unknown as SVGSVGElement;
    const start = { button: 0, clientX: 0, clientY: 0 };
    const move = { clientX: -100000, clientY: 0, currentTarget: svg };
    c.panStart(start as unknown as PointerEvent);
    c.panMove(move as unknown as PointerEvent);
    const v = c.vb();
    const b = (c as unknown as { layout: () => { w: number; h: number } }).layout();
    // slack = half the view width — never further than that off-content
    expect(v.x).toBeGreaterThanOrEqual(-v.w * 0.5 - 1);
    expect(v.x).toBeLessThanOrEqual(b.w - v.w * 0.5 + 1);
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
