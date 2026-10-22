import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { KeyValuePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import {
  Api, Bottleneck, CriticalPath, ExecutionGraph, GraphEdge, GraphNode,
} from './api';

/** Hard cap on drawn nodes — beyond it the SVG would degrade and the
 *  honest answer is a truncation banner, not a slow render. The drawn
 *  set is a BFS-connected prefix from the root (plus user-revealed
 *  nodes), not an arbitrary slice, so what renders is navigable. */
const MAX_NODES = 500;
const NODE_W = 190;
const NODE_H = 36;
const GAP_X = 70;
const GAP_Y = 14;
const PAD = 24;
/** Viewport margin for culling — overdraw this fraction of the viewBox
 *  on every side so panning doesn't pop nodes in at the edge. */
const CULL_PAD = 0.4;
/** Camera clamp slack — the view may scroll up to half its size past
 *  the content bounds, never further (no viewport escape). */
const PAN_SLACK = 0.5;
/** Window width `centerOn` zooms to when the camera is fit to a graph
 *  too large to read — roughly one layer column plus context. */
const READ_W = NODE_W * 8;

interface LNode { n: GraphNode; x: number; y: number; }
interface LEdge {
  e: GraphEdge; x1: number; y1: number; x2: number; y2: number;
  /** `out` = drawn source, off-view target; `in` = off-view source,
   *  drawn target; null = a real edge between two drawn nodes. */
  stub: 'out' | 'in' | null;
}
interface ViewBox { x: number; y: number; w: number; h: number }
/** One side of the selected node's adjacency — `node` is absent for
 *  `dangling` edges whose endpoint isn't in this graph (evidence, not
 *  an error); `drawn` flags ids outside the current cap. */
interface NeighborRef {
  id: string; node: GraphNode | null; drawn: boolean; kind: string;
}

/** Distinct, theme-agnostic stroke per span kind — the node fill stays
 *  `var(--panel)` so dark/light both read correctly; `error` overrides
 *  to `var(--err)` like the waterfall bars. */
const KIND_STROKE: Record<string, string> = {
  agent: '#e36209', chain: '#8b5cf6', llm: '#4cc2ff', tool: '#2ea44f',
  retriever: '#d4a72c', embedding: '#6e7681', reranker: '#bf6edb',
  guardrail: '#f85149', evaluator: '#3fb950',
  server: '#39c5cf', client: '#8250df', producer: '#d29922',
  consumer: '#daaa3f', internal: '#6e7681', trace: '#4cc2ff',
  other: '#6e7681',
};

/** Traversal order used to prioritize the drawn set under MAX_NODES:
 *  BFS from `rootId` (or the first node) over drawable edges; nodes in
 *  disconnected components follow in input order, each rooted at the
 *  first unvisited node. Returns id → rank; every node gets a rank. */
export function bfsOrder(
  nodes: GraphNode[], edges: GraphEdge[], rootId: string | null,
): Map<string, number> {
  const ids = new Set(nodes.map(n => n.id));
  const adj = new Map<string, string[]>();
  for (const n of nodes) adj.set(n.id, []);
  for (const e of edges) {
    if (e.dangling || e.source === e.target) continue;
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    adj.get(e.source)!.push(e.target);
  }
  const rank = new Map<string, number>();
  const visit = (seed: string): void => {
    const q = [seed];
    rank.set(seed, rank.size);
    for (let i = 0; i < q.length; i++) {
      for (const v of adj.get(q[i]) ?? []) {
        if (!rank.has(v)) { rank.set(v, rank.size); q.push(v); }
      }
    }
  };
  if (rootId && ids.has(rootId)) visit(rootId);
  for (const n of nodes) if (!rank.has(n.id)) visit(n.id);
  return rank;
}

/** Longest-path layering: level(v) = 1 + max level of v's drawn
 *  predecessors, sources at 0. Kahn-style over the drawable edges
 *  (non-dangling, both ends in the node set); nodes the sort can't
 *  reach — parent-cycle members, orphans — land in one extra layer at
 *  the right, which is honest rather than wrong. */
export function layeredPositions(
  nodes: GraphNode[], edges: GraphEdge[],
): { pos: Map<string, { x: number; y: number }>; w: number; h: number } {
  const ids = new Set(nodes.map(n => n.id));
  const indeg = new Map<string, number>();
  const adj = new Map<string, string[]>();
  for (const n of nodes) { indeg.set(n.id, 0); adj.set(n.id, []); }
  for (const e of edges) {
    if (e.dangling || e.source === e.target) continue;
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    adj.get(e.source)!.push(e.target);
    indeg.set(e.target, (indeg.get(e.target) ?? 0) + 1);
  }
  const level = new Map<string, number>();
  const q = nodes.filter(n => indeg.get(n.id) === 0).map(n => n.id);
  for (const id of q) level.set(id, 0);
  const seen = new Set<string>();
  for (let i = 0; i < q.length; i++) {
    const u = q[i];
    if (seen.has(u)) continue;
    seen.add(u);
    const lu = level.get(u) ?? 0;
    for (const v of adj.get(u) ?? []) {
      level.set(v, Math.max(level.get(v) ?? 0, lu + 1));
      const d = (indeg.get(v) ?? 1) - 1;
      indeg.set(v, d);
      if (d === 0) q.push(v);
    }
  }
  let maxLevel = -1;
  for (const l of level.values()) if (l > maxLevel) maxLevel = l;
  let leftover = false;
  for (const n of nodes) {
    if (!seen.has(n.id)) { level.set(n.id, maxLevel + 1); leftover = true; }
  }
  if (leftover) maxLevel += 1; // cycle members/orphans — one extra layer
  const byLevel = new Map<number, GraphNode[]>();
  for (const n of nodes) {
    const l = level.get(n.id) ?? 0;
    const arr = byLevel.get(l);
    if (arr) arr.push(n); else byLevel.set(l, [n]);
  }
  const maxRows = Math.max(1, ...[...byLevel.values()].map(a => a.length));
  const pos = new Map<string, { x: number; y: number }>();
  for (const [l, arr] of byLevel) {
    const dy = NODE_H + GAP_Y;
    const offset = (maxRows - arr.length) * dy / 2; // vertically center short layers
    arr.forEach((n, i) => pos.set(n.id, {
      x: PAD + l * (NODE_W + GAP_X),
      y: PAD + offset + i * dy,
    }));
  }
  return {
    pos,
    w: PAD * 2 + (maxLevel + 1) * (NODE_W + GAP_X),
    h: PAD * 2 + maxRows * (NODE_H + GAP_Y),
  };
}

@Component({
  selector: 'lens-graph',
  imports: [RouterLink, FormsModule, KeyValuePipe],
  styles: [`
    .controls { display: flex; gap: .4rem; align-items: center;
      margin-bottom: .6rem; flex-wrap: wrap; }
    .wrap { display: flex; gap: 1rem; align-items: flex-start; }
    svg { flex: 1; min-width: 0; height: 560px; background: var(--panel);
      border-radius: 6px; cursor: grab; touch-action: none; }
    svg:active { cursor: grabbing; }
    .edge { stroke: color-mix(in srgb, var(--fg) 45%, transparent);
      stroke-width: 1.5; fill: none; }
    .edge.seq { stroke-dasharray: 6 4; }
    .edge.lnk { stroke-dasharray: 2 4; }
    .edge.stub { stroke-dasharray: 2 3; }
    .edge.out { stroke: #2ea44f; stroke-width: 2.5; }
    .edge.in { stroke: #d4a72c; stroke-width: 2.5; }
    .edge.cp { stroke: var(--accent); stroke-width: 3; }
    marker path { fill: color-mix(in srgb, var(--fg) 45%, transparent); }
    .node { cursor: pointer; }
    .node rect { fill: var(--panel); stroke-width: 1.5; }
    .node text { fill: var(--fg); pointer-events: none; }
    .nm { font-size: 11px; }
    .kd { font-size: 9px; opacity: .7; }
    .node.err rect { fill: color-mix(in srgb, var(--err) 18%, transparent);
      stroke: var(--err); }
    .node.cp rect { stroke: var(--accent); stroke-width: 3; }
    .node.sel rect { stroke-width: 3; }
    .node.hit rect { stroke: var(--accent); stroke-dasharray: 4 3; }
    aside { width: 300px; flex: none; }
    aside table { font-size: .85em; }
    aside tbody tr { cursor: pointer; }
    .nb { display: block; width: 100%; text-align: left; border: 0;
      background: none; padding: .1rem 0; cursor: pointer;
      color: var(--fg); }
    .nb:hover { color: var(--accent); }
    .nb .bid { opacity: .55; font-size: .85em; }
    .nb.undrawn { opacity: .75; font-style: italic; }
    .nb.dangling { opacity: .55; font-style: italic; cursor: default; }
    .nb.dangling:hover { color: var(--fg); }
    .statline { display: flex; gap: 1rem; flex-wrap: wrap; margin: .4rem 0; }
    .statline span { white-space: nowrap; }
  `],
  template: `
  <h1>Graph Explorer</h1>
  <form class="filters" (ngSubmit)="submit()">
    <select [(ngModel)]="mode" name="mode" aria-label="graph subject">
      <option value="trace">trace</option>
      <option value="session">session</option>
    </select>
    <input [(ngModel)]="subjectInput" name="subject" type="search" size="34"
           [placeholder]="mode === 'trace' ? 'trace id' : 'session id'"
           aria-label="graph subject id" />
    <button type="submit" [disabled]="!subjectInput.trim()">load graph</button>
  </form>
  @if (error()) { <p class="err-banner">{{ error() }}</p> }
  @else if (!graph()) {
    <p class="muted">enter a {{ mode }} id — or deep-link with
      <code>?trace_id=…</code> / <code>?session_id=…</code>.</p>
  }
  @if (graph(); as g) {
    <p class="statline muted">
      <span>{{ g.subject_id }}</span>
      @for (kv of g.stats | keyvalue; track kv.key) {
        <span>{{ kv.key }}: {{ kv.value }}</span>
      }
      <span>drawn {{ drawn().length }} of {{ g.nodes.length }}
        · in view {{ vlnodes().length }}</span>
    </p>
    @if (serverTruncated()) {
      <p class="err-banner">session graph truncated server-side — the
        trace page cap was reached; this is a partial view.</p> }
    @if (clientTruncated()) {
      <p class="err-banner">graph truncated: showing {{ cap }} of
        {{ g.nodes.length }} nodes (BFS prefix from the root) — the rest
        is not drawn; search or "expand neighbors" reveals nodes beyond
        the cap.</p> }
    <div class="controls">
      <button (click)="zoom(0.8)" aria-label="zoom in">+</button>
      <button (click)="zoom(1.25)" aria-label="zoom out">−</button>
      <button (click)="fit()">fit</button>
      <button (click)="reset()">reset view</button>
      <input [ngModel]="findInput" (ngModelChange)="onFindInput($event)"
             name="find" type="search" size="16"
             placeholder="find node (name/id)" aria-label="find node"
             (keydown.enter)="find()" />
      <button (click)="find()" [disabled]="!matches().length">find</button>
      @if (matches().length > 1) {
        <button (click)="findNext()" aria-label="next match">
          next {{ findCursor() + 1 }}/{{ matches().length }}</button> }
      @if (findQuery() && !matches().length) {
        <span class="err-banner">no match for “{{ findQuery() }}”</span> }
      @if (mode === 'trace') {
        <button [class.on]="cpOn()" (click)="toggleCp()">critical path</button>
        @if (cp(); as c) {
          <span class="muted">path {{ c.total_ms }}ms ·
            {{ c.path.length }} nodes
            @if (c.cycles.length) { · {{ c.cycles.length }} cut cycle edge(s) }</span>
        }
      }
      <span class="muted">drag to pan · wheel to zoom</span>
    </div>
    <div class="wrap">
      <svg [attr.viewBox]="viewBox()" role="img" aria-label="execution graph"
           (wheel)="onWheel($event)"
           (pointerdown)="panStart($event)" (pointermove)="panMove($event)"
           (pointerup)="panEnd()" (pointerleave)="panEnd()">
        <defs>
          <marker id="arr" viewBox="0 0 10 10" refX="9" refY="5"
                  markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" />
          </marker>
        </defs>
        @for (le of vledges(); track $index) {
          <line [attr.x1]="le.x1" [attr.y1]="le.y1"
                [attr.x2]="le.x2" [attr.y2]="le.y2" class="edge"
                [class.seq]="le.e.kind === 'sequence'"
                [class.lnk]="le.e.kind === 'link'"
                [class.stub]="le.stub"
                [class.cp]="cpEdgeSet().has(le.e.source + '>' + le.e.target)"
                [class.out]="sel()?.id === le.e.source"
                [class.in]="sel()?.id === le.e.target"
                [attr.marker-end]="le.stub === 'out' ? null : 'url(#arr)'" />
        }
        @for (ln of vlnodes(); track ln.n.id) {
          <g class="node" [class.err]="ln.n.error" [class.cp]="cpSet().has(ln.n.id)"
             [class.sel]="sel()?.id === ln.n.id"
             [class.hit]="matchSet().has(ln.n.id)" (click)="pick(ln.n)">
            <title>{{ ln.n.name }} ({{ ln.n.id }})</title>
            <rect [attr.x]="ln.x" [attr.y]="ln.y"
                  [attr.width]="NW" [attr.height]="NH" rx="5"
                  [attr.stroke]="kindStroke(ln.n.kind)" />
            <text class="nm" [attr.x]="ln.x + 8" [attr.y]="ln.y + 15">
              {{ clip(ln.n.name) }}</text>
            <text class="kd" [attr.x]="ln.x + 8" [attr.y]="ln.y + 29">
              {{ ln.n.kind }} · {{ fmtMs(ln.n.duration_ms) }}</text>
          </g>
        }
      </svg>
      <aside>
        @if (sel(); as s) {
          <section class="panel">
            <h3>{{ s.name }}</h3>
            <p><code>{{ s.id }}</code></p>
            <p>kind {{ s.kind }} · status {{ s.status }} ·
               {{ fmtMs(s.duration_ms) }}</p>
            <p>{{ s.start_time }} → {{ s.end_time ?? 'open' }}</p>
            @if (s.integrity_flags.length) {
              <p>integrity flags: {{ s.integrity_flags.join(', ') }}</p> }
            @if (s.has_token_usage) { <p>token usage recorded</p> }
            @if (s.error) { <p class="err-banner">status error</p> }
            @if (mode === 'session') {
              <a [routerLink]="['/traces', s.id]">open trace →</a>
            } @else {
              <a [routerLink]="['/traces', g.subject_id]">open trace detail →</a>
            }
            @if (selLinks(); as nb) {
              @if (nb.in.length) {
                <h4>← {{ nb.in.length }} in-edge(s)</h4>
                @for (r of nb.in; track $index) {
                  @if (r.node) {
                    <button class="nb" [class.undrawn]="!r.drawn"
                            (click)="go(r.id)">{{ clip(r.node.name) }}
                      <span class="bid">{{ r.kind }}</span></button>
                  } @else {
                    <span class="nb dangling">{{ r.id }}
                      <span class="bid">dangling</span></span>
                  }
                }
              }
              @if (nb.out.length) {
                <h4>→ {{ nb.out.length }} out-edge(s)</h4>
                @for (r of nb.out; track $index) {
                  @if (r.node) {
                    <button class="nb" [class.undrawn]="!r.drawn"
                            (click)="go(r.id)">{{ clip(r.node.name) }}
                      <span class="bid">{{ r.kind }}</span></button>
                  } @else {
                    <span class="nb dangling">{{ r.id }}
                      <span class="bid">dangling</span></span>
                  }
                }
              }
            }
            @if (hiddenNeighbors() > 0) {
              <button (click)="expandSel()">expand neighbors
                ({{ hiddenNeighbors() }} beyond cap)</button> }
          </section>
        }
        @if (mode === 'trace' && bottlenecks().length) {
          <section class="panel">
            <h3>bottlenecks — self time</h3>
            <table>
              <thead><tr><th>name</th><th>kind</th>
                <th>self ms</th><th>total ms</th></tr></thead>
              <tbody>
                @for (b of bottlenecks(); track b.id) {
                  <tr (click)="go(b.id)" [class.err]="isErr(b.id)">
                    <td>{{ b.name }}</td><td>{{ b.kind }}</td>
                    <td>{{ b.self_ms }}</td>
                    <td>{{ b.duration_ms ?? '—' }}</td>
                  </tr>
                }
              </tbody>
            </table>
          </section>
        }
      </aside>
    </div>
  }`,
})
export class GraphComponent implements OnInit {
  private api = inject(Api);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  mode: 'trace' | 'session' = 'trace';
  subjectInput = '';
  subjectId = '';
  findInput = '';
  graph = signal<ExecutionGraph | null>(null);
  error = signal<string | null>(null);
  sel = signal<GraphNode | null>(null);
  cp = signal<CriticalPath | null>(null);
  cpOn = signal(false);
  bottlenecks = signal<Bottleneck[]>([]);
  vb = signal<ViewBox>({ x: 0, y: 0, w: 1000, h: 600 });
  /** Node ids the user explicitly asked to see (search hits, expanded
   *  neighborhoods, bottleneck picks) — drawn even when they would be
   *  cut by MAX_NODES, at the cost of the lowest-priority tail. */
  revealed = signal<string[]>([]);
  findQuery = signal('');
  findCursor = signal(0);
  readonly NW = NODE_W;
  readonly NH = NODE_H;
  readonly cap = MAX_NODES;
  private panLast: { x: number; y: number } | null = null;

  private edges = computed(() => this.graph()?.edges ?? []);
  private nodeById = computed(() => {
    const m = new Map<string, GraphNode>();
    for (const n of this.graph()?.nodes ?? []) m.set(n.id, n);
    return m;
  });
  /** Traversal rank over the FULL node set — computed once per load,
   *  not per change-detection pass. */
  private order = computed(() => {
    const g = this.graph();
    return g ? bfsOrder(g.nodes, g.edges, g.root_id) : new Map<string, number>();
  });
  /** Drawn node set — revealed nodes first, then BFS-priority fill up
   *  to MAX_NODES. Revealing beyond the cap evicts the BFS tail, never
   *  exceeds the cap, and never hides a node the user asked for. */
  drawn = computed(() => {
    const g = this.graph();
    if (!g) return [];
    const byId = this.nodeById();
    const rank = this.order();
    const picked = new Set<string>();
    const out: GraphNode[] = [];
    for (const id of this.revealed()) {
      const n = byId.get(id);
      if (n && !picked.has(id) && out.length < MAX_NODES) {
        picked.add(id); out.push(n);
      }
    }
    const rest = [...g.nodes].sort(
      (a, b) => (rank.get(a.id) ?? 0) - (rank.get(b.id) ?? 0));
    for (const n of rest) {
      if (out.length >= MAX_NODES) break;
      if (!picked.has(n.id)) { picked.add(n.id); out.push(n); }
    }
    return out;
  });
  private drawnIds = computed(() => new Set(this.drawn().map(n => n.id)));
  clientTruncated = computed(() =>
    (this.graph()?.nodes.length ?? 0) > this.drawn().length);
  serverTruncated = computed(() => this.graph()?.stats['truncated'] === true);
  cpSet = computed<Set<string>>(() =>
    new Set(this.cpOn() ? (this.cp()?.path ?? []) : []));
  /** Consecutive pairs along the critical path — edges get the same
   *  accent stroke as path nodes so the chain reads as a chain. */
  cpEdgeSet = computed<Set<string>>(() => {
    const s = new Set<string>();
    if (!this.cpOn()) return s;
    const p = this.cp()?.path ?? [];
    for (let i = 1; i < p.length; i++) s.add(`${p[i - 1]}>${p[i]}`);
    return s;
  });
  /** Layout runs lazily and is cached by the signal graph — pan, zoom,
   *  selection and search never recompute positions; only a graph load
   *  or a reveal-set change does. */
  private layout = computed(() =>
    layeredPositions(this.drawn(), this.edges()));
  lnodes = computed<LNode[]>(() => {
    const pos = this.layout().pos;
    return this.drawn().map(n => ({ n, ...pos.get(n.id)! }));
  });
  /** Edges to undrawn/dangling endpoints render as one short stub per
   *  node per direction — never dropped silently (stats keep the real
   *  counts) but never duplicated either: in a dense graph a drawn node
   *  may hold dozens of off-view edges and one marker is the honest
   *  summary of "there is more here". */
  ledges = computed<LEdge[]>(() => {
    const pos = this.layout().pos;
    const les: LEdge[] = [];
    const stubbed = new Set<string>();
    for (const e of this.edges()) {
      const s = pos.get(e.source);
      const t = pos.get(e.target);
      if (s && t) {
        les.push({ e, x1: s.x + NODE_W, y1: s.y + NODE_H / 2,
          x2: t.x, y2: t.y + NODE_H / 2, stub: null });
      } else if (s && !stubbed.has(`o${e.source}`)) {
        stubbed.add(`o${e.source}`);
        les.push({ e, x1: s.x + NODE_W, y1: s.y + NODE_H / 2,
          x2: s.x + NODE_W + 34, y2: s.y + NODE_H / 2, stub: 'out' });
      } else if (t && !stubbed.has(`i${e.target}`)) {
        stubbed.add(`i${e.target}`);
        les.push({ e, x1: t.x - 34, y1: t.y + NODE_H / 2,
          x2: t.x, y2: t.y + NODE_H / 2, stub: 'in' });
      }
    }
    return les;
  });
  /** Viewport culling — only nodes intersecting the padded viewBox are
   *  instantiated; edges pass a conservative bbox test (a segment whose
   *  bbox misses the viewport cannot cross it), so nothing visible is
   *  ever culled. DOM churn on pan is incremental: only entering and
   *  leaving elements are added/removed by the @for diff. */
  vlnodes = computed<LNode[]>(() => {
    const v = this.vb();
    const px = v.w * CULL_PAD;
    const py = v.h * CULL_PAD;
    return this.lnodes().filter(l =>
      l.x + NODE_W >= v.x - px && l.x <= v.x + v.w + px
      && l.y + NODE_H >= v.y - py && l.y <= v.y + v.h + py);
  });
  vledges = computed<LEdge[]>(() => {
    const v = this.vb();
    const px = v.w * CULL_PAD;
    const py = v.h * CULL_PAD;
    return this.ledges().filter(le =>
      Math.max(le.x1, le.x2) >= v.x - px && Math.min(le.x1, le.x2) <= v.x + v.w + px
      && Math.max(le.y1, le.y2) >= v.y - py
      && Math.min(le.y1, le.y2) <= v.y + v.h + py);
  });
  viewBox = computed(() => {
    const v = this.vb();
    return `${v.x} ${v.y} ${v.w} ${v.h}`;
  });
  /** Search runs over the full payload, not just the drawn slice — a
   *  hit beyond the cap is revealed, then centered. */
  matches = computed<string[]>(() => {
    const q = this.findQuery().toLowerCase();
    if (!q) return [];
    return (this.graph()?.nodes ?? [])
      .filter(n => n.id.toLowerCase().includes(q)
        || n.name.toLowerCase().includes(q))
      .map(n => n.id);
  });
  matchSet = computed(() => new Set(this.matches()));
  /** Selected node's in/out neighbors (drawn or not) plus dangling
   *  endpoints — the traverse surface. */
  selLinks = computed<{ in: NeighborRef[]; out: NeighborRef[] }>(() => {
    const s = this.sel();
    const res = { in: [] as NeighborRef[], out: [] as NeighborRef[] };
    if (!s) return res;
    const byId = this.nodeById();
    const drawn = this.drawnIds();
    for (const e of this.edges()) {
      if (e.target === s.id) {
        res.in.push({ id: e.source, node: byId.get(e.source) ?? null,
          drawn: drawn.has(e.source), kind: e.kind });
      }
      if (e.source === s.id) {
        res.out.push({ id: e.target, node: byId.get(e.target) ?? null,
          drawn: drawn.has(e.target), kind: e.kind });
      }
    }
    const byIdKey = (a: NeighborRef, b: NeighborRef): number =>
      a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    res.in.sort(byIdKey); res.out.sort(byIdKey);
    return res;
  });
  /** Adjacent node ids that exist in the payload but are cut by the
   *  cap — the expand affordance's count. */
  hiddenNeighbors = computed(() => {
    const nb = this.selLinks();
    const ids = new Set<string>();
    for (const r of [...nb.in, ...nb.out]) {
      if (r.node && !r.drawn) ids.add(r.id);
    }
    return ids.size;
  });

  ngOnInit(): void {
    const pm = this.route.snapshot.queryParamMap;
    const tid = pm.get('trace_id');
    const sid = pm.get('session_id');
    if (tid) { this.mode = 'trace'; this.subjectInput = tid; this.load(); }
    else if (sid) { this.mode = 'session'; this.subjectInput = sid; this.load(); }
  }

  submit(): void { this.load(); }

  load(): void {
    const id = this.subjectInput.trim();
    if (!id) return;
    this.subjectId = id;
    this.error.set(null);
    this.sel.set(null);
    this.cp.set(null);
    this.cpOn.set(false);
    this.bottlenecks.set([]);
    this.revealed.set([]);
    this.onFindInput('');
    const obs = this.mode === 'trace'
      ? this.api.getTraceGraph(id) : this.api.getSessionGraph(id);
    obs.subscribe({
      next: g => {
        this.graph.set(g);
        this.fit();
        // keep the URL deep-linkable (absolute commands — `[]` would
        // resolve against the root route and navigate off /graph)
        void this.router.navigate(['/graph'], {
          queryParams: this.mode === 'trace'
            ? { trace_id: id } : { session_id: id },
          replaceUrl: true,
        });
      },
      error: (e: HttpErrorResponse) => {
        this.graph.set(null);
        this.error.set(
          `cannot load ${this.mode} graph (${e.status || e.message})`);
      },
    });
    if (this.mode === 'trace') {
      this.api.getBottlenecks(id, 10).subscribe({
        next: r => this.bottlenecks.set(r.items),
        error: () => this.bottlenecks.set([]), // panel simply hides
      });
    }
  }

  fit(): void {
    const { w, h } = this.layout();
    this.vb.set({ x: 0, y: 0, w, h });
  }
  /** Clear all navigation overlays — selection, search, revealed
   *  expansion — and refit the camera to the content. */
  reset(): void {
    this.sel.set(null);
    this.revealed.set([]);
    this.onFindInput('');
    this.fit();
  }
  /** Uniform zoom about the viewBox centre; f < 1 zooms in. */
  zoom(f: number): void {
    const v = this.vb();
    const b = this.layout();
    const w = Math.min(Math.max(v.w * f, 60), b.w * 4);
    const h = v.h * (w / v.w);
    this.vb.set(this.clampVb({
      x: v.x + (v.w - w) / 2, y: v.y + (v.h - h) / 2, w, h,
    }));
  }
  onWheel(ev: WheelEvent): void {
    ev.preventDefault();
    this.zoom(ev.deltaY > 0 ? 1.2 : 1 / 1.2);
  }
  panStart(ev: PointerEvent): void {
    if (ev.button === 0) this.panLast = { x: ev.clientX, y: ev.clientY };
  }
  panMove(ev: PointerEvent): void {
    if (!this.panLast) return;
    const r = (ev.currentTarget as SVGSVGElement).getBoundingClientRect();
    const v = this.vb();
    const dx = (ev.clientX - this.panLast.x) * v.w / Math.max(1, r.width);
    const dy = (ev.clientY - this.panLast.y) * v.h / Math.max(1, r.height);
    this.panLast = { x: ev.clientX, y: ev.clientY };
    this.vb.set(this.clampVb({ ...v, x: v.x - dx, y: v.y - dy }));
  }
  panEnd(): void { this.panLast = null; }

  /** Keep the camera inside content ± a half-viewport slack — panning
   *  can never lose the graph off-screen. */
  private clampVb(v: ViewBox): ViewBox {
    const b = this.layout();
    const sx = Math.min(v.w * PAN_SLACK, b.w);
    const sy = Math.min(v.h * PAN_SLACK, b.h);
    const lo = { x: -sx, y: -sy };
    const hi = { x: b.w - v.w + sx, y: b.h - v.h + sy };
    return {
      w: v.w, h: v.h,
      x: hi.x < lo.x ? (b.w - v.w) / 2
        : Math.min(Math.max(v.x, lo.x), hi.x),
      y: hi.y < lo.y ? (b.h - v.h) / 2
        : Math.min(Math.max(v.y, lo.y), hi.y),
    };
  }
  /** Center the camera on a laid-out node; if the current window is a
   *  fit-all view too wide to read, zoom in to a readable column. */
  centerOn(id: string): void {
    const p = this.layout().pos.get(id);
    if (!p) return;
    const v = this.vb();
    const b = this.layout();
    const w = v.w > READ_W ? Math.min(READ_W, b.w) : v.w;
    // keep the window readable vertically too — a wide flat graph
    // (a chain) would otherwise zoom to a pixel-high sliver
    const h = Math.max(w * v.h / v.w, Math.min(NODE_H * 6, b.h));
    this.vb.set(this.clampVb({
      x: p.x + NODE_W / 2 - w / 2, y: p.y + NODE_H / 2 - h / 2, w, h,
    }));
  }

  pick(n: GraphNode): void { this.sel.set(n); }
  /** Navigate to any node id — draws it first if the cap hid it, then
   *  selects and centers. Used by neighbor lists, bottlenecks and the
   *  find box, so no node the UI offers is ever unreachable. */
  go(id: string): void {
    const n = this.nodeById().get(id);
    if (!n) return;
    if (!this.drawnIds().has(id)) this.revealed.update(r => [...r, id]);
    this.sel.set(n);
    this.centerOn(id);
  }
  /** Reveal every adjacent node the cap hid — the drawn set stays ≤
   *  MAX_NODES (the BFS tail is evicted), never a silent drop. */
  expandSel(): void {
    const nb = this.selLinks();
    const add: string[] = [];
    for (const r of [...nb.in, ...nb.out]) {
      if (r.node && !r.drawn && !add.includes(r.id)) add.push(r.id);
    }
    if (add.length) this.revealed.update(r => [...r, ...add]);
  }
  /** Live query — the match count and `find` enablement update as the
   *  user types; the scan is a substring pass over the full payload. */
  onFindInput(v: string): void {
    this.findInput = v;
    this.findQuery.set(v.trim().toLowerCase());
    this.findCursor.set(0);
  }
  find(): void {
    const m = this.matches();
    if (!m.length) return;
    this.findCursor.set(0);
    this.go(m[0]);
  }
  findNext(): void {
    const m = this.matches();
    if (!m.length) return;
    this.findCursor.update(c => (c + 1) % m.length);
    this.go(m[this.findCursor()]);
  }
  isErr(id: string): boolean {
    return this.nodeById().get(id)?.error ?? false;
  }

  toggleCp(): void {
    if (this.cp()) { this.cpOn.update(v => !v); return; }
    this.api.getCriticalPath(this.subjectId).subscribe({
      next: c => { this.cp.set(c); this.cpOn.set(true); },
      error: (e: HttpErrorResponse) =>
        this.error.set(`critical path unavailable (${e.status || e.message})`),
    });
  }

  kindStroke(kind: string): string { return KIND_STROKE[kind] ?? '#6e7681'; }
  clip(name: string): string {
    return name.length > 26 ? `${name.slice(0, 25)}…` : name;
  }
  fmtMs(ms: number | null | undefined): string {
    return ms == null ? '—' : `${Math.round(ms)}ms`;
  }
}
