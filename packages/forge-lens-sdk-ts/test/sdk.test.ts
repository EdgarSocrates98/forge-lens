import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  HTTPExporter,
  InMemoryExporter,
  LensTracer,
  formatTraceparent,
  isValidSpanId,
  isValidTraceId,
  newSpanId,
  newTraceId,
  parseTraceparent,
} from "../src/index.js";
import type { Span, Trace, TraceIngest } from "../src/index.js";

describe("ids", () => {
  it("generates W3C-shaped ids", () => {
    const tid = newTraceId();
    const sid = newSpanId();
    assert.ok(isValidTraceId(tid));
    assert.ok(isValidSpanId(sid));
    assert.ok(!isValidTraceId("g".repeat(32)));
    assert.ok(!isValidTraceId("0".repeat(32)));
  });

  it("traceparent round-trips", () => {
    const ctx = { traceId: newTraceId(), spanId: newSpanId(), sampled: true };
    const parsed = parseTraceparent(formatTraceparent(ctx));
    assert.deepEqual(parsed, ctx);
    assert.equal(parseTraceparent("garbage"), null);
  });

  it("sampled is bit 0x01, not the last hex char", () => {
    const tid = newTraceId();
    const sid = newSpanId();
    assert.equal(parseTraceparent(`00-${tid}-${sid}-03`)!.sampled, true);
    assert.equal(parseTraceparent(`00-${tid}-${sid}-ff`)!.sampled, true);
    assert.equal(parseTraceparent(`00-${tid}-${sid}-02`)!.sampled, false);
    assert.equal(parseTraceparent(`00-${tid}-${sid}-f0`)!.sampled, false);
  });

  it("rejects version ff and all-zero ids (W3C)", () => {
    const tid = newTraceId();
    const sid = newSpanId();
    assert.equal(parseTraceparent(`ff-${tid}-${sid}-01`), null);
    assert.equal(parseTraceparent(`00-${"0".repeat(32)}-${sid}-01`), null);
    assert.equal(parseTraceparent(`00-${tid}-${"0".repeat(16)}-01`), null);
  });
});

describe("LensTracer", () => {
  it("nests spans and auto-flushes on root end", async () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "test", exporter, shutdownHook: false });
    tracer.agent("root", (root) => {
      tracer.llmCall("claude-test", (llm) => {
        tracer.recordUsage(llm, { input_tokens: 10, output_tokens: 4 });
      });
      tracer.toolCall("search", () => {});
    });
    assert.equal(exporter.traces.length, 1);
    const trace = exporter.traces[0];
    assert.equal(trace.spans.length, 3);
    const rootSpan = trace.spans.find((s) => s.name === "root")!;
    assert.equal(trace.root_span_id, rootSpan.span_id);
    for (const s of trace.spans) {
      if (s !== rootSpan) assert.equal(s.parent_span_id, rootSpan.span_id);
    }
    const llm = trace.spans.find((s) => s.kind === "llm")!;
    const usage = llm.attributes["forge_lens.token_usage"] as Record<string, unknown>;
    assert.equal(usage.input_tokens, 10);
  });

  it("marks errors and records exception events", async () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "t", exporter, shutdownHook: false });
    assert.throws(() =>
      tracer.span("boom", () => {
        throw new Error("nope");
      }),
    );
    const trace = exporter.traces[0];
    assert.equal(trace.status.code, "error");
    assert.equal(trace.spans[0].status.code, "error");
    assert.equal(trace.spans[0].events[0].attributes?.["exception.type"], "Error");
  });

  it("supports async spans", async () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "t", exporter, shutdownHook: false });
    await tracer.agent("root", async () => {
      await tracer.llmCall("m", async () => {
        await new Promise((r) => setTimeout(r, 5));
      });
    });
    assert.equal(exporter.traces.length, 1);
    assert.equal(exporter.traces[0].spans.length, 2);
    assert.equal(exporter.traces[0].status.code, "ok");
  });

  it("continues a remote parent trace", () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "t", exporter, shutdownHook: false });
    const remote = { traceId: newTraceId(), spanId: newSpanId(), sampled: true };
    tracer.span("local", () => {}, { remoteParent: remote });
    const trace = exporter.traces[0];
    assert.equal(trace.trace_id, remote.traceId);
    assert.equal(trace.spans[0].parent_span_id, remote.spanId);
  });

  it("keeps the batch pending when export fails, retry succeeds", async () => {
    let calls = 0;
    const exporter = {
      async export(traces: Trace[]) {
        calls += 1;
        if (calls === 1) throw new Error("endpoint down");
        return { accepted_spans: traces.length };
      },
    };
    const tracer = new LensTracer({ serviceName: "t", exporter, autoFlush: false, shutdownHook: false });
    tracer.span("s", () => {});
    await assert.rejects(() => tracer.flush(), /endpoint down/);
    const ok = await tracer.flush(); // retry — batch was retained
    assert.deepEqual(ok, { accepted_spans: 1 });
  });

  it("auto-flush rejection is caught, counted and reported — never unhandled", async () => {
    const seen: unknown[] = [];
    const exporter = {
      async export(): Promise<never> {
        throw new Error("endpoint down");
      },
    };
    const tracer = new LensTracer({
      serviceName: "t",
      exporter,
      onExportError: (err) => seen.push(err),
      maxExportFailures: 100,
      shutdownHook: false,
    });
    tracer.agent("root", () => {}); // fire-and-forget flush rejects inside
    await new Promise((r) => setTimeout(r, 10)); // let the rejection settle
    assert.equal(tracer.exportErrors, 1);
    assert.equal(seen.length, 1);
    assert.equal((seen[0] as Error).message, "endpoint down");
  });

  it("auto-flush failure does not mask an app exception", async () => {
    const exporter = {
      async export(): Promise<never> {
        throw new Error("endpoint down");
      },
    };
    const tracer = new LensTracer({ serviceName: "t", exporter, shutdownHook: false });
    assert.throws(
      () =>
        tracer.span("root", () => {
          throw new Error("app-bug");
        }),
      /app-bug/,
    );
    await new Promise((r) => setTimeout(r, 10));
    assert.equal(tracer.exportErrors, 1);
  });

  it("mid-flight flush() keeps root auto-flush registered", async () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "t", exporter, shutdownHook: false });
    await tracer.span("root", async () => {
      tracer.span("child1", () => {});
      await tracer.flush(); // documented mid-flight flush (partial delivery)
      assert.equal(exporter.traces.length, 1);
      tracer.span("child2", () => {});
    });
    // root ended → auto-flush queued behind the chain; this await drains it
    await tracer.flush();
    assert.equal(exporter.traces.length, 2);
    const names = exporter.traces[1].spans.map((s) => s.name);
    for (const n of ["root", "child1", "child2"]) {
      assert.ok(names.includes(n), `missing span ${n}`);
    }
  });

  it("shutdown() exports traces still pending", async () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({
      serviceName: "t",
      exporter,
      autoFlush: false,
      shutdownHook: false,
    });
    tracer.span("s", () => {});
    await tracer.shutdown();
    assert.equal(exporter.traces.length, 1);
    assert.equal(exporter.traces[0].spans[0].name, "s");
  });

  it("a poison trace cannot block the rest of the batch", async () => {
    const exporter = {
      traces: [] as Trace[],
      async export(batch: Trace[]) {
        for (const t of batch) {
          if (t.spans.some((s) => s.name === "poison")) {
            throw new Error("simulated permanent rejection (413)");
          }
        }
        this.traces.push(...batch);
        return {};
      },
    };
    const tracer = new LensTracer({
      serviceName: "t",
      exporter,
      autoFlush: false,
      maxExportFailures: 5,
      shutdownHook: false,
    });
    tracer.span("good", () => {});
    tracer.span("poison", () => {});
    await assert.rejects(() => tracer.flush());
    // salvage pass re-exported each trace alone — the good one landed
    assert.equal(exporter.traces.length, 1);
    assert.equal(exporter.traces[0].spans[0].name, "good");
  });

  it("persistent export failure dead-letters explicitly", async () => {
    const exporter = {
      async export(): Promise<never> {
        throw new Error("permanent");
      },
    };
    const dead: Trace[] = [];
    const tracer = new LensTracer({
      serviceName: "t",
      exporter,
      autoFlush: false,
      maxExportFailures: 2,
      onDeadLetter: (t) => dead.push(t),
      shutdownHook: false,
    });
    tracer.span("doomed", () => {});
    await assert.rejects(() => tracer.flush());
    await assert.rejects(() => tracer.flush());
    assert.equal(dead.length, 1);
    assert.equal(dead[0].spans[0].name, "doomed");
    assert.equal(tracer.deadLetters.length, 1);
  });

  it("finalize orders offset ISO timestamps by instant, not lexically", async () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "t", exporter, autoFlush: false, shutdownHook: false });
    let rootSpan: Span | undefined;
    let childSpan: Span | undefined;
    tracer.agent("root", (root) => {
      rootSpan = root;
      tracer.span("child", (c) => {
        childSpan = c;
      });
    });
    // earliest instant, but lexicographically the LATEST start string
    rootSpan!.start_time = "2026-06-01T10:00:00+12:00"; // = 2026-05-31T22:00Z
    childSpan!.start_time = "2026-06-01T01:00:00.000Z";
    // latest instant, but lexicographically the SMALLER end string
    rootSpan!.end_time = "2026-06-01T02:00:00.000Z";
    childSpan!.end_time = "2026-06-01T12:00:00+12:00"; // = 2026-06-01T00:00Z
    await tracer.flush();
    const t = exporter.traces[0];
    assert.equal(t.started_at, "2026-06-01T10:00:00+12:00");
    assert.equal(t.ended_at, "2026-06-01T02:00:00.000Z");
  });
});

describe("HTTPExporter", () => {
  it("posts a TraceIngest envelope", async () => {
    let captured: { url: string; headers: Headers; body: TraceIngest } | null = null;
    const fakeFetch = (async (url: string | URL | Request, init?: RequestInit) => {
      captured = {
        url: String(url),
        headers: new Headers(init?.headers),
        body: JSON.parse(String(init?.body)) as TraceIngest,
      };
      return new Response('{"accepted_spans": 1}', { status: 200 });
    }) as typeof fetch;

    const exporter = new HTTPExporter({
      endpoint: "http://127.0.0.1:9999/",
      apiKey: "k",
      projectId: "p1",
      fetchImpl: fakeFetch,
    });
    const mem = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "t", exporter: mem, autoFlush: false, shutdownHook: false });
    tracer.span("s", () => {});
    await tracer.flush();
    await exporter.export(mem.traces, tracer.source);
    assert.equal(captured!.url, "http://127.0.0.1:9999/v1/traces");
    assert.equal(captured!.headers.get("x-forge-lens-key"), "k");
    assert.equal(captured!.body.schema_version, "1.0.0");
    assert.equal(captured!.body.project_id, "p1");
    assert.equal(captured!.body.traces[0].spans[0].name, "s");
  });
});

describe("redact hook", () => {
  it("runs the redact hook per-span before export", async () => {
    const mem = new InMemoryExporter();
    const tracer = new LensTracer({
      serviceName: "t", exporter: mem, shutdownHook: false,
      redact: (s) => { s.attributes["secret"] = "[REDACTED]"; },
    });
    tracer.span("s", () => {}, { attributes: { secret: "sk-ant-xyz" } });
    await tracer.flush();
    assert.equal(mem.traces[0].spans[0].attributes["secret"], "[REDACTED]");
  });

  it("a throwing redact hook is counted, never thrown", async () => {
    const mem = new InMemoryExporter();
    const tracer = new LensTracer({
      serviceName: "t", exporter: mem, shutdownHook: false,
      redact: () => { throw new Error("boom"); },
    });
    tracer.span("s", () => {});
    await tracer.flush();
    assert.equal(tracer.exportErrors, 1);
    assert.equal(mem.traces.length, 1);
  });
});

describe("flush serialization", () => {
  it("a flush() landing between a run's end and a queued run's start does not interleave", async () => {
    // Regression: flushRunning was cleared in run1's finally BEFORE the
    // queued run's microtask started — a flush() called in that gap took
    // the fast path and ran a concurrent export.
    let concurrent = 0;
    let maxConcurrent = 0;
    let releaseExport: (() => void) | null = null;
    let releaseBody: (() => void) | null = null;
    const gate = new Promise<void>((r) => { releaseExport = r; });
    const holdBody = new Promise<void>((r) => { releaseBody = r; });
    const exporter = {
      async export(): Promise<unknown> {
        concurrent++;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        // yield a microtask tick even after the gate opens — two runs
        // starting back-to-back would overlap their exports here
        await gate;
        await Promise.resolve();
        concurrent--;
        return { accepted_spans: 0 };
      },
    };
    const tracer = new LensTracer({
      serviceName: "t", exporter, autoFlush: false, shutdownHook: false,
    });
    // gated async span body → trace stays pending across all three
    // flushes (a dropped trace would export nothing and hide the race)
    const bodyDone = tracer.span("long", async () => { await holdBody; });
    const f1 = tracer.flush();              // run1 in-flight, export gated
    const gapFlush = f1.then(() => tracer.flush()); // lands in the gap (old bug)
    const f2 = tracer.flush();              // queued behind run1
    releaseExport!();
    await Promise.all([f1, gapFlush, f2]);
    assert.equal(maxConcurrent, 1);
    releaseBody!();
    await bodyDone;
  });
});

describe("InMemoryExporter bound", () => {
  it("caps retained traces and counts overflow", () => {
    const exp = new InMemoryExporter(2);
    const mk = (i: number) => ({
      trace_id: `${i}`.repeat(32), spans: [] as never[],
    });
    exp.export([mk(1), mk(2)] as never[]);
    exp.export([mk(3), mk(4), mk(5)] as never[]);
    assert.equal(exp.traces.length, 2);
    assert.equal(exp.tracesDropped, 3);
  });
});
