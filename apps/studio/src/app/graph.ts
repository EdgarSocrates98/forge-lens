import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { KeyValuePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import {
  Api, Bottleneck, CriticalPath, ExecutionGraph, GraphEdge, GraphNode,
} from './api';

/** Hard cap on drawn nodes — beyond it the SVG would degrade and the
 *  honest answer is a truncation banner, not a slow render. */
const MAX_NODES = 500;
const NODE_W = 190;
const NODE_H = 36;
const GAP_X = 70;
const GAP_Y = 14;
const PAD = 24;

interface LNode { n: GraphNode; x: number; y: number; }
interface LEdge { e: GraphEdge; x1: number; y1: number; x2: number; y2: number; stub: boolean }
interface ViewBox { x: number; y: number; w: number; h: number }

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

/** Longest-path layering: level(v) = 1 + max level of v's drawn
 *  predecessors, sources at 0. Kahn-style over the drawable edges
 *  (non-dangling, both ends in the node set); nodes the sort can't
 *  reach — parent-cycle members, orphans — land in one extra layer at
 *  the right, which is honest rather than wrong. */
function layeredPositions(
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
    aside { width: 300px; flex: none; }
    aside table { font-size: .85em; }
    aside tbody tr { cursor: pointer; }
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
    </p>
    @if (serverTruncated()) {
      <p class="err-banner">session graph truncated server-side — the
        trace page cap was reached; this is a partial view.</p> }
    @if (clientTruncated()) {
      <p class="err-banner">graph truncated: showing {{ cap }} of
        {{ g.nodes.length }} nodes — the rest is not drawn.</p> }
    <div class="controls">
      <button (click)="zoom(0.8)" aria-label="zoom in">+</button>
      <button (click)="zoom(1.25)" aria-label="zoom out">−</button>
      <button (click)="fit()">fit</button>
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
        @for (le of ledges(); track $index) {
          <line [attr.x1]="le.x1" [attr.y1]="le.y1"
                [attr.x2]="le.x2" [attr.y2]="le.y2" class="edge"
                [class.seq]="le.e.kind === 'sequence'"
                [class.lnk]="le.e.kind === 'link'"
                [class.stub]="le.stub"
                [attr.marker-end]="le.stub ? null : 'url(#arr)'" />
        }
        @for (ln of lnodes(); track ln.n.id) {
          <g class="node" [class.err]="ln.n.error" [class.cp]="cpSet().has(ln.n.id)"
             [class.sel]="sel()?.id === ln.n.id" (click)="pick(ln.n)">
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
                  <tr (click)="pickById(b.id)" [class.err]="isErr(b.id)">
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
  graph = signal<ExecutionGraph | null>(null);
  error = signal<string | null>(null);
  sel = signal<GraphNode | null>(null);
  cp = signal<CriticalPath | null>(null);
  cpOn = signal(false);
  bottlenecks = signal<Bottleneck[]>([]);
  lnodes = signal<LNode[]>([]);
  ledges = signal<LEdge[]>([]);
  vb = signal<ViewBox>({ x: 0, y: 0, w: 1000, h: 600 });
  readonly NW = NODE_W;
  readonly NH = NODE_H;
  readonly cap = MAX_NODES;
  private bounds = { w: 1000, h: 600 };
  private panLast: { x: number; y: number } | null = null;

  /** Drawn node set — the first MAX_NODES in server order. */
  private drawn = computed(() => this.graph()?.nodes.slice(0, MAX_NODES) ?? []);
  clientTruncated = computed(() =>
    (this.graph()?.nodes.length ?? 0) > MAX_NODES);
  serverTruncated = computed(() => this.graph()?.stats['truncated'] === true);
  cpSet = computed<Set<string>>(() =>
    new Set(this.cpOn() ? (this.cp()?.path ?? []) : []));
  viewBox = computed(() => {
    const v = this.vb();
    return `${v.x} ${v.y} ${v.w} ${v.h}`;
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
    const obs = this.mode === 'trace'
      ? this.api.getTraceGraph(id) : this.api.getSessionGraph(id);
    obs.subscribe({
      next: g => {
        this.graph.set(g);
        this.layout();
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

  /** Compute layered positions over the drawn slice and fit the viewBox
   *  to the resulting bounds. Edges to undrawn/dangling targets render
   *  as short stubs — never dropped silently (stats keep the counts). */
  private layout(): void {
    const ns = this.drawn();
    const { pos, w, h } = layeredPositions(ns, this.graph()?.edges ?? []);
    this.lnodes.set(ns.map(n => ({ n, x: pos.get(n.id)!.x, y: pos.get(n.id)!.y })));
    const les: LEdge[] = [];
    for (const e of this.graph()?.edges ?? []) {
      const s = pos.get(e.source);
      const t = pos.get(e.target);
      if (s && t) {
        les.push({ e, x1: s.x + NODE_W, y1: s.y + NODE_H / 2,
          x2: t.x, y2: t.y + NODE_H / 2, stub: false });
      } else if (s) {
        les.push({ e, x1: s.x + NODE_W, y1: s.y + NODE_H / 2,
          x2: s.x + NODE_W + 34, y2: s.y + NODE_H / 2, stub: true });
      }
    }
    this.ledges.set(les);
    this.bounds = { w, h };
    this.fit();
  }

  fit(): void {
    this.vb.set({ x: 0, y: 0, w: this.bounds.w, h: this.bounds.h });
  }
  /** Uniform zoom about the viewBox centre; f < 1 zooms in. */
  zoom(f: number): void {
    const v = this.vb();
    const w = Math.min(Math.max(v.w * f, 60), this.bounds.w * 4);
    const h = v.h * (w / v.w);
    this.vb.set({
      x: v.x + (v.w - w) / 2, y: v.y + (v.h - h) / 2, w, h,
    });
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
    this.vb.set({ ...v, x: v.x - dx, y: v.y - dy });
  }
  panEnd(): void { this.panLast = null; }

  pick(n: GraphNode): void { this.sel.set(n); }
  pickById(id: string): void {
    const n = this.drawn().find(x => x.id === id);
    if (n) this.sel.set(n);
  }
  isErr(id: string): boolean {
    return this.drawn().find(x => x.id === id)?.error ?? false;
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
