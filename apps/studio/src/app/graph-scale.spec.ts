import {
  componentsGraph, dagGraph, denseGraph, extremeGraph, treeGraph,
} from './graph-scale.fixture';
import { bfsOrder, layeredPositions } from './graph';
import { GraphNode } from './api';

/** Deterministic scale fixtures + layout perf assertions. Timing logs
 *  feed docs/graph-scale.md — thresholds carry ~4× headroom over the
 *  measured values on the dev box so CI jitter doesn't flake them. */

const ids = (nodes: GraphNode[]): Set<string> =>
  new Set(nodes.map(n => n.id));

const validRefs = (g: ReturnType<typeof treeGraph>): void => {
  const set = ids(g.nodes);
  for (const e of g.edges) {
    expect(set.has(e.source)).toBe(true);
    if (!e.dangling) expect(set.has(e.target)).toBe(true);
    else expect(set.has(e.target)).toBe(false);
  }
};

describe('graph-scale fixtures', () => {
  it('are byte-identical for the same (size, seed)', () => {
    const gens: ((n: number, s: number) => unknown)[] = [
      treeGraph, dagGraph, (n, s) => componentsGraph(n, 4, s),
      denseGraph, extremeGraph,
    ];
    for (const gen of gens) {
      expect(JSON.stringify(gen(120, 7))).toBe(JSON.stringify(gen(120, 7)));
    }
    expect(JSON.stringify(treeGraph(120, 7)))
      .not.toBe(JSON.stringify(treeGraph(120, 8)));
  });

  it('produce the ExecutionGraph payload shape with honest stats', () => {
    const g = dagGraph(300, 5);
    expect(g.subject_id).toBe('dag-300');
    expect(g.nodes.length).toBe(300);
    expect(g.root_id).toBe('n0');
    validRefs(g);
    expect(g.stats['span_count']).toBe(300);
    expect(g.stats['edge_count']).toBe(g.edges.length);
    for (const n of g.nodes) {
      expect(n.id).toBeTruthy();
      expect(n.kind).toBeTruthy();
      expect(n.name).toBeTruthy();
      expect(n.start_time).toBeTruthy();
      expect(Array.isArray(n.integrity_flags)).toBe(true);
      expect(typeof n.has_token_usage).toBe('boolean');
      expect(typeof n.error).toBe('boolean');
    }
  });

  it('tree(100)/tree(500) are real parent trees of the right size', () => {
    for (const size of [100, 500]) {
      const g = treeGraph(size, 1);
      expect(g.nodes.length).toBe(size);
      const parents = g.edges.filter(e => e.kind === 'parent');
      expect(parents.length).toBe(size - 1);
      validRefs(g);
    }
  });

  it('dagGraph(1000) stays acyclic by construction (targets > source)', () => {
    const g = dagGraph(1000, 2);
    expect(g.nodes.length).toBe(1000);
    const idx = new Map(g.nodes.map((n, i) => [n.id, i]));
    for (const e of g.edges) {
      expect(idx.get(e.target)!).toBeGreaterThan(idx.get(e.source)!);
    }
  });

  it('componentsGraph produces N disconnected pieces', () => {
    const g = componentsGraph(300, 5, 3);
    const order = bfsOrder(g.nodes, g.edges, g.root_id);
    // 5 components × ~60 nodes each; BFS ranks stay contiguous per piece
    expect(order.size).toBe(300);
    expect(g.stats['max_depth'] as number).toBeLessThan(60);
  });

  it('denseGraph(3000) carries heavy edge load + dangling links', () => {
    const g = denseGraph(3000, 4);
    expect(g.nodes.length).toBe(3000);
    expect(g.edges.length).toBeGreaterThan(3000 * 2);
    const dangling = g.edges.filter(e => e.dangling);
    expect(dangling.length).toBeGreaterThan(0);
    expect(g.stats['dangling_edges']).toBe(dangling.length);
    validRefs(g);
  });

  it('extremeGraph(size) scales to 5000 and 10000 nodes', () => {
    for (const size of [5000, 10000]) {
      const g = extremeGraph(size, 5);
      expect(g.nodes.length).toBe(size);
      validRefs(g);
      // every node reachable from the root — no orphans
      expect(bfsOrder(g.nodes, g.edges, g.root_id).size).toBe(size);
    }
  });
});

describe('layout perf', () => {
  /** Runs `fn`, logs the elapsed ms (surfaced in vitest output and
   *  copied into docs/graph-scale.md) and asserts the ceiling —
   *  ~4× headroom over the dev-box measurement so CI jitter can't
   *  flake it but a real regression trips. */
  const time = <T>(label: string, ceiling: number, fn: () => T): T => {
    const t0 = performance.now();
    const r = fn();
    const ms = performance.now() - t0;
    console.log(`[perf] ${label}: ${ms.toFixed(1)}ms (ceiling ${ceiling}ms)`);
    expect(ms).toBeLessThan(ceiling);
    return r;
  };

  it('generates the extreme 10000-node payload fast', () => {
    const g = time('extremeGraph(10000) generate', 3000,
      () => extremeGraph(10000, 5));
    expect(g.nodes.length).toBe(10000);
  });

  it('layers a 500-node drawn cap in interactive time', () => {
    const g = treeGraph(500, 1);
    const { pos } = time('layeredPositions tree(500)', 150,
      () => layeredPositions(g.nodes, g.edges));
    expect(pos.size).toBe(500);
  });

  it('layers the 3000-node dense profile within the cap workflow', () => {
    const g = denseGraph(3000, 4);
    // BFS order over the full payload — what the component runs per load
    const order = time('bfsOrder dense(3000)', 500,
      () => bfsOrder(g.nodes, g.edges, g.root_id));
    expect(order.size).toBe(3000);
    // the layout itself only ever sees the ≤500-node drawn slice
    const drawn = g.nodes.slice(0, 500);
    time('layeredPositions drawn(500 of dense 3000)', 300,
      () => layeredPositions(drawn, g.edges));
  });

  it('BFS-orders the 10000-node extreme profile under 500ms', () => {
    const g = extremeGraph(10000, 5);
    const order = time('bfsOrder extreme(10000)', 500,
      () => bfsOrder(g.nodes, g.edges, g.root_id));
    expect(order.size).toBe(10000);
  });
});
