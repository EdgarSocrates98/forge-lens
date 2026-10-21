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
import type { Trace, TraceIngest } from "../src/index.js";

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
});

describe("LensTracer", () => {
  it("nests spans and auto-flushes on root end", async () => {
    const exporter = new InMemoryExporter();
    const tracer = new LensTracer({ serviceName: "test", exporter });
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
    const tracer = new LensTracer({ serviceName: "t", exporter });
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
    const tracer = new LensTracer({ serviceName: "t", exporter });
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
    const tracer = new LensTracer({ serviceName: "t", exporter });
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
    const tracer = new LensTracer({ serviceName: "t", exporter, autoFlush: false });
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
    const tracer = new LensTracer({ serviceName: "t", exporter });
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
    const tracer = new LensTracer({ serviceName: "t", exporter: mem, autoFlush: false });
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
