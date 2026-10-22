import { ExecutionGraph, GraphEdge, GraphNode } from './api';

/**
 * Deterministic scale fixtures for the Graph Explorer — pure generators,
 * zero dependencies, seeded. The same `(size, seed)` pair always yields a
 * byte-identical payload: callers can diff, cache and time them safely.
 *
 * Payload shape mirrors `GET /v1/graph/{traces,sessions}/{id}` exactly
 * (`ExecutionGraph` from `./api`), including the `stats` counters the UI
 * surfaces as evidence (`span_count`, `edge_count`, `max_depth`,
 * `dangling_edges`, `missing_parents`, `cycle_members`).
 */

/** mulberry32 — small fast seeded PRNG; returns floats in [0, 1). */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Span kinds weighted like a real agent trace — agents/tools/llms
 *  dominate; the long tail keeps every stroke color exercised. */
const KINDS = [
  'internal', 'internal', 'tool', 'tool', 'llm', 'llm', 'agent',
  'chain', 'retriever', 'embedding', 'reranker', 'guardrail',
  'evaluator', 'server', 'client', 'producer', 'consumer', 'other',
];

const EPOCH = Date.UTC(2026, 0, 1);

/** Deterministic node attrs — ~5% errors, ~8% open (no end), a few
 *  integrity flags and token-usage markers, all driven by `rand`. */
function makeNode(i: number, rand: () => number): GraphNode {
  const kind = KINDS[Math.floor(rand() * KINDS.length)];
  const start = EPOCH + i * 37;
  const open = rand() < 0.08;
  const duration = Math.round(rand() * 4800 + 12);
  const error = rand() < 0.05;
  const flags: string[] = [];
  const fr = rand();
  if (fr < 0.02) flags.push('missing_parent');
  else if (fr < 0.04) flags.push('clock_skew');
  return {
    id: `n${i}`,
    kind,
    name: `${kind} op ${i}`,
    start_time: new Date(start).toISOString(),
    end_time: open ? null : new Date(start + duration).toISOString(),
    duration_ms: open ? null : duration,
    status: error ? 'error' : 'ok',
    integrity_flags: flags,
    has_token_usage: kind === 'llm' && rand() < 0.7,
    error,
  };
}

function graph(
  subject: string, nodes: GraphNode[], edges: GraphEdge[],
  extra: Record<string, unknown> = {},
): ExecutionGraph {
  const dangling = edges.filter(e => e.dangling).length;
  return {
    subject_id: subject, project_id: 'fixture', root_id: nodes[0]?.id ?? null,
    nodes, edges,
    stats: {
      span_count: nodes.length, edge_count: edges.length,
      max_depth: maxDepth(nodes, edges), dangling_edges: dangling,
      missing_parents: 0, cycle_members: 0, ...extra,
    },
  };
}

/** Longest-path depth over drawable edges — same layering rule the UI
 *  uses, so `max_depth` agrees with what the renderer draws. */
function maxDepth(nodes: GraphNode[], edges: GraphEdge[]): number {
  const ids = new Set(nodes.map(n => n.id));
  const adj = new Map<string, string[]>();
  const indeg = new Map<string, number>();
  for (const n of nodes) { adj.set(n.id, []); indeg.set(n.id, 0); }
  for (const e of edges) {
    if (e.dangling || e.source === e.target
      || !ids.has(e.source) || !ids.has(e.target)) continue;
    adj.get(e.source)!.push(e.target);
    indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1);
  }
  const level = new Map<string, number>();
  const q = nodes.filter(n => indeg.get(n.id) === 0).map(n => n.id);
  for (const id of q) level.set(id, 0);
  let best = 0;
  for (let i = 0; i < q.length; i++) {
    const u = q[i];
    const lu = level.get(u) ?? 0;
    if (lu > best) best = lu;
    for (const v of adj.get(u) ?? []) {
      level.set(v, Math.max(level.get(v) ?? 0, lu + 1));
      const d = (indeg.get(v) ?? 1) - 1;
      indeg.set(v, d);
      if (d === 0) q.push(v);
    }
  }
  return best;
}

/** `size` nodes in a parent tree — each node i>0 hangs under
 *  `n[floor((i-1)/fanout)]`; siblings also get a `sequence` edge in
 *  id order, mirroring build_trace_graph's shape. */
export function treeGraph(size: number, seed = 1, fanout = 2): ExecutionGraph {
  const rand = mulberry32(seed);
  const nodes = Array.from({ length: size }, (_, i) => makeNode(i, rand));
  const edges: GraphEdge[] = [];
  for (let i = 1; i < size; i++) {
    const p = Math.floor((i - 1) / fanout);
    edges.push({ source: `n${p}`, target: `n${i}`, kind: 'parent' });
    if (i % fanout !== 1) {
      edges.push({ source: `n${i - 1}`, target: `n${i}`, kind: 'sequence' });
    }
  }
  return graph(`tree-${size}`, nodes, edges);
}

/** `size` nodes in a layered DAG — ~sqrt(size) columns; every node draws
 *  1–3 seeded edges into later columns (skipped with small probability so
 *  layers aren't perfectly saturated). */
export function dagGraph(size: number, seed = 2): ExecutionGraph {
  const rand = mulberry32(seed);
  const nodes = Array.from({ length: size }, (_, i) => makeNode(i, rand));
  const width = Math.max(2, Math.round(Math.sqrt(size)));
  const edges: GraphEdge[] = [];
  for (let i = 0; i < size; i++) {
    const col = Math.floor(i / width);
    const fan = 1 + Math.floor(rand() * 3);
    for (let k = 0; k < fan; k++) {
      if (rand() < 0.15) continue;
      const tcol = col + 1 + Math.floor(rand() * 2);
      const t = tcol * width + Math.floor(rand() * width);
      if (t < size && t !== i) {
        edges.push({ source: `n${i}`, target: `n${t}`, kind: 'parent' });
      }
    }
  }
  return graph(`dag-${size}`, nodes, edges);
}

/** `size` nodes split into `components` disconnected chains — the first
 *  node of each component is a root; nothing connects components. */
export function componentsGraph(
  size: number, components: number, seed = 3,
): ExecutionGraph {
  const rand = mulberry32(seed);
  const nodes = Array.from({ length: size }, (_, i) => makeNode(i, rand));
  const edges: GraphEdge[] = [];
  const per = Math.max(1, Math.floor(size / components));
  for (let i = 1; i < size; i++) {
    if (i % per === 0 && i / per <= components - 1) continue; // new root
    edges.push({ source: `n${i - 1}`, target: `n${i}`, kind: 'parent' });
    if (rand() < 0.3) {
      edges.push({ source: `n${i - 1}`, target: `n${i}`, kind: 'link' });
    }
  }
  return graph(`components-${size}x${components}`, nodes, edges);
}

/** `size` nodes, dense edge load — a chain backbone plus 3–5 seeded
 *  `sequence`/`link` edges per node to nearby targets, and ~1% `dangling`
 *  link edges pointing outside the graph (stub-rendered by the UI). */
export function denseGraph(size: number, seed = 4): ExecutionGraph {
  const rand = mulberry32(seed);
  const nodes = Array.from({ length: size }, (_, i) => makeNode(i, rand));
  const edges: GraphEdge[] = [];
  for (let i = 1; i < size; i++) {
    edges.push({ source: `n${i - 1}`, target: `n${i}`, kind: 'parent' });
  }
  const seen = new Set(edges.map(e => `${e.source}>${e.target}`));
  for (let i = 0; i < size; i++) {
    const extra = 3 + Math.floor(rand() * 3);
    for (let k = 0; k < extra; k++) {
      if (rand() < 0.01) {
        edges.push({ source: `n${i}`, target: `ghost-${i}-${k}`,
          kind: 'link', dangling: true });
        continue;
      }
      const t = i + 1 + Math.floor(rand() * 24);
      const key = `n${i}>n${t}`;
      if (t < size && !seen.has(key)) {
        seen.add(key);
        edges.push({ source: `n${i}`, target: `n${t}`,
          kind: rand() < 0.4 ? 'link' : 'sequence' });
      }
    }
  }
  return graph(`dense-${size}`, nodes, edges);
}

/** `size` nodes shaped to stress the layerer: a deep backbone chain with
 *  a seeded burst of children every `stride` nodes (each burst chains a
 *  few levels deep) — depth and breadth both grow with `size`. Use for
 *  the 5 000/10 000-node extreme profiles. */
export function extremeGraph(size: number, seed = 5,
  stride = 10): ExecutionGraph {
  const rand = mulberry32(seed);
  const nodes = Array.from({ length: size }, (_, i) => makeNode(i, rand));
  const edges: GraphEdge[] = [];
  // backbone consumes ~size/4 ids; bursts consume the rest
  const backbone = Math.max(1, Math.floor(size / 4));
  let next = backbone;
  for (let i = 1; i < backbone; i++) {
    edges.push({ source: `n${i - 1}`, target: `n${i}`, kind: 'parent' });
  }
  for (let i = stride; i < backbone && next < size; i += stride) {
    const burst = 2 + Math.floor(rand() * 3);
    for (let b = 0; b < burst && next < size; b++) {
      let prev = i;
      const depth = 1 + Math.floor(rand() * 4);
      for (let d = 0; d < depth && next < size; d++) {
        edges.push({ source: `n${prev}`, target: `n${next}`, kind: 'parent' });
        prev = next;
        next++;
      }
    }
  }
  // any leftover ids chain off the last backbone node — no orphans
  while (next < size) {
    edges.push({ source: `n${backbone - 1}`, target: `n${next}`,
      kind: 'parent' });
    next++;
  }
  return graph(`extreme-${size}`, nodes, edges);
}
