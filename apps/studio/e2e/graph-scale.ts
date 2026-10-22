/**
 * ≥3k-node ExecutionGraph payload for the route-intercepted scale spec —
 * standalone (the src/app generator pulls in Angular's api.ts chain,
 * which the Playwright/esbuild runtime shouldn't load). Mirrors
 * `GET /v1/graph/traces/{id}` exactly: ExecutionGraph from src/app/api.
 *
 * Shape: a fanout-2 parent tree + sibling sequence edges — the same
 * structure build_trace_graph emits for a wide agent run. The BFS draw
 * cap (500) covers the shallow levels, so high-numbered nodes sit
 * beyond the cap and only navigation (find) reveals them.
 */

interface GNode {
  id: string;
  kind: string;
  name: string;
  start_time: string;
  end_time: string | null;
  duration_ms: number | null;
  status: string;
  integrity_flags: string[];
  has_token_usage: boolean;
  error: boolean;
}

interface GEdge {
  source: string;
  target: string;
  kind: string;
  dangling?: boolean;
}

const KINDS = ['agent', 'chain', 'tool', 'llm', 'retriever', 'internal'];

export function bigTraceGraph(size: number, subjectId: string) {
  const epoch = Date.UTC(2026, 0, 1);
  const nodes: GNode[] = [];
  for (let i = 0; i < size; i++) {
    const kind = KINDS[i % KINDS.length];
    const dur = 12 + ((i * 7919) % 4800);
    const open = i % 13 === 12;
    const error = i % 19 === 18;
    nodes.push({
      id: `n${i}`,
      kind,
      name: `${kind} op ${i}`,
      start_time: new Date(epoch + i * 37).toISOString(),
      end_time: open ? null : new Date(epoch + i * 37 + dur).toISOString(),
      duration_ms: open ? null : dur,
      status: error ? 'error' : 'ok',
      integrity_flags: [],
      has_token_usage: kind === 'llm' && i % 3 === 0,
      error,
    });
  }
  const edges: GEdge[] = [];
  for (let i = 1; i < size; i++) {
    edges.push({ source: `n${Math.floor((i - 1) / 2)}`, target: `n${i}`, kind: 'parent' });
    if (i % 2 === 0) {
      edges.push({ source: `n${i - 1}`, target: `n${i}`, kind: 'sequence' });
    }
  }
  return {
    subject_id: subjectId,
    project_id: 'scale-e2e',
    root_id: 'n0',
    nodes,
    edges,
    stats: {
      span_count: nodes.length,
      edge_count: edges.length,
      max_depth: Math.ceil(Math.log2(size + 1)) - 1,
      dangling_edges: 0,
      missing_parents: 0,
      cycle_members: 0,
    },
  };
}
