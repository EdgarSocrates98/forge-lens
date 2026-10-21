/** LensTracer — accumulate spans per trace and flush through an exporter. */

import type {
  SCHEMA_VERSION,
  Span,
  SpanEvent,
  SpanKind,
  StatusCode,
  Trace,
  TraceIngest,
  TraceSource,
} from "./contracts.js";
import {
  isValidTraceId,
  newSpanId,
  newTraceId,
  type SpanContext,
} from "./ids.js";
import { currentSpan, withSpan } from "./context.js";

export interface Exporter {
  export(traces: Trace[], source?: TraceSource): Promise<unknown> | unknown;
}

export class InMemoryExporter implements Exporter {
  readonly traces: Trace[] = [];
  /** Overflow past `maxTraces` — counted, never silently dropped. */
  tracesDropped = 0;
  constructor(readonly maxTraces = 1000) {
    // coerce like Python int() ("5" → 5), then fail fast on garbage:
    // null/""/non-finite must not silently zero the capacity
    // (Number(null)===0 — checked before coercion, like int(None))
    if (
      maxTraces == null ||
      (typeof (maxTraces as unknown) === "string" &&
        (maxTraces as unknown as string).trim() === "") ||
      !Number.isFinite(Number(maxTraces))
    ) {
      throw new TypeError(`maxTraces must be finite, got ${maxTraces}`);
    }
    this.maxTraces = Math.max(0, Math.trunc(Number(maxTraces)));
  }
  export(
    traces: Trace[],
  ): {
    accepted_spans: number;
    traces: string[];
    submitted_traces: number;
    traces_dropped: number;
  } {
    const room = this.maxTraces - this.traces.length;
    const kept = traces.slice(0, Math.max(0, room));
    this.traces.push(...kept);
    const dropped = traces.length - kept.length;
    this.tracesDropped += dropped;
    // accepted = actually retained — same convention as the ingest pipeline
    return {
      accepted_spans: kept.reduce((n, t) => n + t.spans.length, 0),
      traces: kept.map((t) => t.trace_id),
      submitted_traces: traces.length,
      traces_dropped: dropped,
    };
  }
}

export interface HTTPExporterOptions {
  endpoint?: string;
  apiKey?: string;
  projectId?: string;
  fetchImpl?: typeof fetch;
}

/** POSTs TraceIngest envelopes via fetch (Node 18+, zero deps). */
export class HTTPExporter implements Exporter {
  private readonly endpoint: string;
  private readonly apiKey?: string;
  private readonly projectId: string;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: HTTPExporterOptions = {}) {
    this.endpoint = (opts.endpoint ?? "http://127.0.0.1:7777").replace(/\/+$/, "");
    this.apiKey = opts.apiKey;
    this.projectId = opts.projectId ?? "default";
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  async export(traces: Trace[], source?: TraceSource): Promise<unknown> {
    const envelope: TraceIngest = {
      schema_version: "1.0.0",
      project_id: this.projectId,
      source: source ?? null,
      traces,
      sent_at: new Date().toISOString(),
    };
    const headers: Record<string, string> = { "content-type": "application/json" };
    if (this.apiKey) headers["x-forge-lens-key"] = this.apiKey;
    const resp = await this.fetchImpl(`${this.endpoint}/v1/traces`, {
      method: "POST",
      headers,
      body: JSON.stringify(envelope),
    });
    if (!resp.ok) {
      const detail = (await resp.text()).slice(0, 500);
      throw new Error(`forge-lens export failed HTTP ${resp.status}: ${detail}`);
    }
    return resp.json();
  }
}

export interface TokenUsage {
  provider?: string;
  model?: string;
  input_tokens?: number;
  output_tokens?: number;
  cached_input_tokens?: number;
  reasoning_tokens?: number;
  total_tokens?: number;
  measurement_source?: "provider" | "estimated" | "unknown";
}

export interface LensTracerOptions {
  serviceName: string;
  sdkVersion?: string;
  exporter?: Exporter;
  autoFlush?: boolean;
  sessionId?: string;
  projectId?: string;
  onFlush?: (trace: Trace) => void;
  /** Called when an auto-flush export fails (batch stays pending). */
  onExportError?: (err: unknown, trace: Trace) => void;
  /** Called when a trace is dead-lettered after maxExportFailures. */
  onDeadLetter?: (trace: Trace) => void;
  /** User-supplied redaction hook: called per-span at flush before the
   *  snapshot is taken, may mutate span attributes — a redacted value
   *  never reaches the exporter. Hook errors are counted on
   *  `exportErrors`, never thrown into the observed app. */
  redact?: (span: Span) => void;
  /** Consecutive export failures before a trace is dead-lettered (default 5). */
  maxExportFailures?: number;
  /** Maximum retained dead letters — overflow is counted on
   *  `deadLettersDropped` so a permanently-broken exporter cannot grow
   *  memory forever (default 1000). */
  maxDeadLetters?: number;
  /** Register a beforeExit hook that best-effort flushes pending traces
   *  (default true). */
  shutdownHook?: boolean;
}

export class LensTracer {
  readonly source: TraceSource;
  readonly projectId: string;
  /** Failed auto-flush exports — observability never breaks the host. */
  exportErrors = 0;
  /** Traces dropped after `maxExportFailures` consecutive export failures —
   *  explicit, inspectable, never silent. Bounded by `maxDeadLetters`. */
  readonly deadLetters: Trace[] = [];
  /** Dead letters that overflowed `maxDeadLetters` — still counted. */
  deadLettersDropped = 0;
  private readonly exporter: Exporter;
  private readonly autoFlush: boolean;
  private readonly sessionId?: string;
  private readonly onFlush?: (trace: Trace) => void;
  private readonly onExportError?: (err: unknown, trace: Trace) => void;
  private readonly onDeadLetter?: (trace: Trace) => void;
  private readonly redact?: (span: Span) => void;
  private readonly maxExportFailures: number;
  private readonly maxDeadLetters: number;
  private readonly pending = new Map<string, Trace>();
  private readonly roots = new Map<string, string>();
  private readonly failures = new Map<string, number>();
  /** Serializes concurrent flush() calls (single-threaded, but async). */
  private flushChain: Promise<unknown> = Promise.resolve();
  private flushRunning = false;
  private flushQueued = 0;

  constructor(opts: LensTracerOptions) {
    this.source = {
      system: opts.serviceName,
      sdk_name: "forge-lens-sdk-typescript",
      sdk_version: opts.sdkVersion ?? "0.1.0",
      runtime: `node-${process.version}`,
      language: "typescript",
    };
    this.exporter = opts.exporter ?? new InMemoryExporter();
    this.autoFlush = opts.autoFlush ?? true;
    this.sessionId = opts.sessionId;
    this.projectId = opts.projectId ?? "default";
    this.onFlush = opts.onFlush;
    this.onExportError = opts.onExportError;
    this.onDeadLetter = opts.onDeadLetter;
    this.redact = opts.redact;
    this.maxExportFailures = Math.max(1, opts.maxExportFailures ?? 5);
    this.maxDeadLetters = Math.max(1, opts.maxDeadLetters ?? 1000);
    if (opts.shutdownHook !== false) {
      process.once("beforeExit", () => {
        void this.flush().catch(() => {
          this.exportErrors += 1; // nothing else can be done at exit
        });
      });
    }
  }

  /** Run `fn` inside a new span. Sync or async `fn` both supported. */
  span<T>(
    name: string,
    fn: (span: Span) => T,
    opts: { kind?: SpanKind; attributes?: Record<string, unknown>; remoteParent?: SpanContext } = {},
  ): T {
    const parent = opts.remoteParent ?? currentSpan();
    const attributes: Record<string, unknown> = { ...(opts.attributes ?? {}) };
    let traceId = parent?.traceId;
    if (traceId !== undefined && !isValidTraceId(traceId)) {
      // flag, don't silently fix: record the discarded producer id
      attributes["forge_lens.trace_id_regenerated"] = true;
      attributes["forge_lens.invalid_trace_id_supplied"] = String(traceId).slice(0, 200);
      traceId = undefined;
    }
    const ctx: SpanContext = {
      traceId: traceId ?? newTraceId(),
      spanId: newSpanId(),
      sampled: true,
    };
    const span: Span = {
      schema_version: "1.0.0",
      trace_id: ctx.traceId,
      span_id: ctx.spanId,
      parent_span_id: parent?.spanId ?? null,
      name,
      kind: opts.kind ?? "internal",
      start_time: new Date().toISOString(),
      end_time: null,
      attributes,
      events: [],
      links: [],
      status: { code: "unset" },
      provenance: "observed",
      integrity_flags: [],
    };
    const trace = this.traceFor(ctx, parent === undefined || opts.remoteParent !== undefined);
    trace.spans.push(span);

    return withSpan(ctx, () => {
      try {
        const result = fn(span);
        if (result instanceof Promise) {
          return result
            .then((v) => {
              this.finish(span, null);
              this.maybeFlush(trace, ctx);
              return v;
            })
            .catch((err) => {
              this.finish(span, err);
              this.maybeFlush(trace, ctx);
              throw err;
            }) as T;
        }
        this.finish(span, null);
        this.maybeFlush(trace, ctx);
        return result;
      } catch (err) {
        this.finish(span, err);
        this.maybeFlush(trace, ctx);
        throw err;
      }
    });
  }

  agent<T>(name: string, fn: (span: Span) => T, attributes: Record<string, unknown> = {}): T {
    return this.span(name, fn, { kind: "agent", attributes });
  }

  llmCall<T>(model: string, fn: (span: Span) => T, attributes: Record<string, unknown> = {}): T {
    return this.span(`llm ${model}`, fn, {
      kind: "llm",
      attributes: { "llm.model_name": model, ...attributes },
    });
  }

  toolCall<T>(toolName: string, fn: (span: Span) => T, attributes: Record<string, unknown> = {}): T {
    return this.span(`tool ${toolName}`, fn, {
      kind: "tool",
      attributes: { "tool.name": toolName, ...attributes },
    });
  }

  recordUsage(span: Span, usage: TokenUsage): void {
    span.attributes["forge_lens.token_usage"] = {
      schema_version: "1.0.0",
      measurement_source: usage.measurement_source ?? "provider",
      ...usage,
    };
  }

  private finish(span: Span, err: unknown): void {
    span.end_time = new Date().toISOString();
    if (err !== null && err !== undefined) {
      span.status = {
        code: "error",
        message: err instanceof Error ? err.message.slice(0, 500) : String(err).slice(0, 500),
      };
      const event: SpanEvent = {
        name: "exception",
        time: new Date().toISOString(),
        attributes: {
          "exception.type": err instanceof Error ? err.name : typeof err,
          "exception.message": String(err).slice(0, 1000),
        },
      };
      span.events.push(event);
    } else if (span.status.code === "unset") {
      span.status = { code: "ok" };
    }
  }

  private traceFor(ctx: SpanContext, isRoot: boolean): Trace {
    let trace = this.pending.get(ctx.traceId);
    if (!trace) {
      trace = {
        schema_version: "1.0.0",
        trace_id: ctx.traceId,
        project_id: this.projectId,
        session_id: this.sessionId ?? null,
        root_span_id: null,
        started_at: new Date().toISOString(),
        ended_at: null,
        status: { code: "unset" },
        source: this.source,
        resource_attributes: {},
        spans: [],
        provenance: "observed",
      };
      this.pending.set(ctx.traceId, trace);
    }
    if (isRoot) {
      this.roots.set(ctx.traceId, ctx.spanId);
      trace.root_span_id = ctx.spanId;
    }
    return trace;
  }

  private maybeFlush(trace: Trace, ctx: SpanContext): void {
    if (this.autoFlush && this.roots.get(ctx.traceId) === ctx.spanId) {
      // fire-and-forget with a catch: a rejected export must never become
      // an unhandled rejection inside the host process
      void this.flush(ctx.traceId).catch((err: unknown) => {
        this.exportErrors += 1;
        if (this.onExportError) {
          try {
            this.onExportError(err, trace);
          } catch {
            this.exportErrors += 1;
          }
        }
      });
    }
  }

  private isFinished(trace: Trace): boolean {
    const root = trace.spans.find((s) => s.span_id === trace.root_span_id);
    if (root !== undefined) return root.end_time !== null;
    // no registered root (e.g. a continued remote trace whose remote
    // parent never lands): finished once every span has ended
    return (
      trace.spans.length > 0 && trace.spans.every((s) => s.end_time !== null)
    );
  }

  private drop(traceId: string): void {
    this.pending.delete(traceId);
    this.roots.delete(traceId);
    this.failures.delete(traceId);
  }

  private markExported(live: Trace): void {
    this.failures.delete(live.trace_id);
    if (this.isFinished(live)) this.drop(live.trace_id);
  }

  private bumpFailure(live: Trace): void {
    const count = (this.failures.get(live.trace_id) ?? 0) + 1;
    if (count >= this.maxExportFailures) {
      // persistently failing (e.g. permanent 413) — dead-letter
      // explicitly, inspectable via `deadLetters`, never silent
      this.drop(live.trace_id);
      if (this.deadLetters.length < this.maxDeadLetters) {
        this.deadLetters.push(live);
      } else {
        this.deadLettersDropped += 1;
      }
      if (this.onDeadLetter) {
        try {
          this.onDeadLetter(live);
        } catch {
          this.exportErrors += 1;
        }
      }
      return;
    }
    this.failures.set(live.trace_id, count);
  }

  /** On batch failure, salvage good traces from a poisoned batch by
   *  re-exporting each alone — one bad apple can't sink the rest. */
  private async handleExportFailure(
    batch: { live: Trace; snapshot: Trace }[],
  ): Promise<void> {
    let failing = batch;
    if (batch.length > 1) {
      failing = [];
      for (const b of batch) {
        try {
          await this.exporter.export([b.snapshot], this.source);
          this.markExported(b.live);
          if (this.onFlush) this.onFlush(b.snapshot);
        } catch {
          failing.push(b);
        }
      }
    }
    for (const b of failing) this.bumpFailure(b.live);
  }

  /** Export pending traces. Serialized through a promise chain so two
   *  flushes never interleave their exports.
   *
   *  A mid-flight flush() does NOT unregister the trace: pending keeps
   *  accumulating and auto-flush still fires on root end (the store
   *  merges the partial deliveries). Failed batches stay pending for
   *  retry; traces failing `maxExportFailures` consecutive exports move
   *  to `deadLetters` (and `onDeadLetter` is called). The error
   *  propagates to the *explicit* caller only. */
  async flush(traceId?: string): Promise<unknown> {
    if (this.flushRunning) {
      // an export is in flight or queued — serialize behind it.
      // flushQueued is counted at queue time and flushRunning stays
      // true while a queued run exists, closing the microtask gap
      // between a run's finally and the queued run's start.
      this.flushQueued++;
      const run = this.flushChain.then(() => {
        this.flushQueued--;
        return this.flushInternal(traceId);
      });
      this.flushChain = run.catch(() => undefined);
      return run;
    }
    // fast path: runs synchronously up to the first await, so a
    // synchronous exporter completes in the same tick as before
    const run = this.flushInternal(traceId);
    this.flushChain = run.catch(() => undefined);
    return run;
  }

  private async flushInternal(traceId?: string): Promise<unknown> {
    this.flushRunning = true;
    try {
      const ids = traceId ? [traceId] : [...this.pending.keys()];
      const batch: { live: Trace; snapshot: Trace }[] = [];
      for (const id of ids) {
        const live = this.pending.get(id);
        if (live) {
          finalize(live);
          if (this.redact !== undefined) {
            for (const s of live.spans) {
              try {
                this.redact(s);
              } catch {
                this.exportErrors += 1;
              }
            }
          }
          // snapshot so later span mutation can't leak into the batch
          batch.push({ live, snapshot: structuredClone(live) });
        }
      }
      if (batch.length === 0) return undefined;
      try {
        const result = await this.exporter.export(
          batch.map((b) => b.snapshot),
          this.source,
        );
        for (const b of batch) {
          this.markExported(b.live);
          if (this.onFlush) this.onFlush(b.snapshot);
        }
        return result;
      } catch (err) {
        await this.handleExportFailure(batch);
        throw err;
      }
    } finally {
      // stay "running" while queued work exists — otherwise a flush()
      // landing between this finally and the queued run's start would
      // take the fast path and interleave its export with it
      this.flushRunning = this.flushQueued > 0;
    }
  }

  /** Terminal flush for process teardown — export everything pending.
   *  Also runs best-effort on `beforeExit` unless shutdownHook=false. */
  async shutdown(): Promise<unknown> {
    return this.flush();
  }
}

/** ISO-8601 compare that honors offsets: "T10:00+12:00" is earlier than
 *  "T01:00Z" even though it sorts later lexicographically. Falls back to
 *  string order when a value can't be parsed. */
function cmpIso(a: string, b: string): number {
  const ta = Date.parse(a);
  const tb = Date.parse(b);
  if (!Number.isNaN(ta) && !Number.isNaN(tb)) return ta - tb;
  return a < b ? -1 : a > b ? 1 : 0;
}

function finalize(trace: Trace): void {
  if (trace.spans.length > 0) {
    trace.started_at = trace.spans
      .map((s) => s.start_time)
      .sort(cmpIso)[0];
    const ended = trace.spans.filter((s) => s.end_time).map((s) => s.end_time as string).sort(cmpIso);
    trace.ended_at = ended.length > 0 ? ended[ended.length - 1] : null;
  }
  const codes = new Set<StatusCode>(trace.spans.map((s) => s.status.code));
  if (codes.has("error")) trace.status = { code: "error" };
  else if (codes.size === 1 && codes.has("ok")) trace.status = { code: "ok" };
  else trace.status = { code: "unset" };
}
