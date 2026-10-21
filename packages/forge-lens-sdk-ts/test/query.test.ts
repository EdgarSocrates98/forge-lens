import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { LensClient, LensQueryError } from "../src/index.js";

interface Captured {
  url: string;
  method: string;
  headers: Headers;
  body: unknown;
  signal?: AbortSignal | null;
}

/** Transport-level mock — same pattern as the HTTPExporter test. */
function fakeFetch(
  handler: (c: Captured) => Response | Promise<Response>,
): { fetchImpl: typeof fetch; calls: Captured[] } {
  const calls: Captured[] = [];
  const fetchImpl = (async (url: string | URL | Request, init?: RequestInit) => {
    const c: Captured = {
      url: String(url),
      method: init?.method ?? "GET",
      headers: new Headers(init?.headers),
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
      signal: init?.signal,
    };
    calls.push(c);
    return handler(c);
  }) as typeof fetch;
  return { fetchImpl, calls };
}

const okJson = (payload: unknown) =>
  new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "content-type": "application/json" },
  });

describe("LensClient transport", () => {
  it("serializes listTraces filters into snake_case query params", async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      okJson({ items: [], limit: 50, offset: 0 }),
    );
    const client = new LensClient({
      endpoint: "http://127.0.0.1:9999/",
      fetchImpl,
    });
    const res = await client.listTraces({
      sessionId: "sess 1",
      status: "error",
      since: new Date("2026-06-01T00:00:00.000Z"),
      until: "2026-06-02T00:00:00Z",
      minDurationMs: 1.5,
      maxDurationMs: 500,
      q: "boom",
      tool: "web_search",
      limit: 10,
      offset: 20,
    });
    assert.deepEqual(res, { items: [], limit: 50, offset: 0 });
    const url = new URL(calls[0].url);
    assert.equal(url.origin + url.pathname, "http://127.0.0.1:9999/v1/traces");
    const p = url.searchParams;
    assert.equal(p.get("project_id"), "default");
    assert.equal(p.get("session_id"), "sess 1");
    assert.equal(p.get("status"), "error");
    assert.equal(p.get("since"), "2026-06-01T00:00:00.000Z");
    assert.equal(p.get("until"), "2026-06-02T00:00:00Z");
    assert.equal(p.get("min_duration_ms"), "1.5");
    assert.equal(p.get("max_duration_ms"), "500");
    assert.equal(p.get("q"), "boom");
    assert.equal(p.get("tool"), "web_search");
    assert.equal(p.get("limit"), "10");
    assert.equal(p.get("offset"), "20");
  });

  it("sends defaults: limit=50/offset=0 on traces, 100 on sessions/tools", async () => {
    const { fetchImpl, calls } = fakeFetch(() =>
      okJson({ items: [], limit: 0, offset: 0 }),
    );
    const client = new LensClient({
      endpoint: "http://x",
      fetchImpl,
    });
    await client.listTraces();
    await client.listSessions();
    await client.listTools();
    const p = (i: number) => new URL(calls[i].url).searchParams;
    assert.equal(p(0).get("limit"), "50");
    assert.equal(p(0).get("offset"), "0");
    assert.equal(p(1).get("limit"), "100");
    assert.equal(p(2).get("limit"), "100");
  });

  it("sends X-Forge-Lens-Key only when apiKey is set", async () => {
    const { fetchImpl, calls } = fakeFetch(() => okJson({ status: "ok" }));
    const authed = new LensClient({
      endpoint: "http://x",
      apiKey: "k-reader",
      fetchImpl,
    });
    await authed.health();
    assert.equal(calls[0].headers.get("x-forge-lens-key"), "k-reader");
    const anon = new LensClient({ endpoint: "http://x", fetchImpl });
    await anon.health();
    assert.equal(calls[1].headers.get("x-forge-lens-key"), null);
  });

  it("per-call projectId overrides the client default", async () => {
    const { fetchImpl, calls } = fakeFetch(() => okJson({}));
    const client = new LensClient({
      endpoint: "http://x",
      projectId: "proj-a",
      fetchImpl,
    });
    await client.traceCost("t1");
    await client.traceCost("t1", { projectId: "proj-b" });
    assert.equal(new URL(calls[0].url).searchParams.get("project_id"), "proj-a");
    assert.equal(new URL(calls[1].url).searchParams.get("project_id"), "proj-b");
  });

  it("URL-encodes path segments (ids are caller data)", async () => {
    const { fetchImpl, calls } = fakeFetch(() => okJson({}));
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    await client.getDatasetVersion("ds/evil", "v1?x", { projectId: "p" });
    assert.equal(
      calls[0].url,
      "http://x/v1/datasets/ds%2Fevil/versions/v1%3Fx?project_id=p",
    );
  });

  it("returns dataset routes as bare arrays, not envelopes", async () => {
    const { fetchImpl } = fakeFetch(() =>
      okJson([{ dataset_id: "ds1" }, { dataset_id: "ds2" }]),
    );
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    const ds = await client.listDatasets();
    assert.ok(Array.isArray(ds));
    assert.equal(ds.length, 2);
    const versions = await client.listDatasetVersions("ds1");
    assert.ok(Array.isArray(versions));
  });

  it("posts read-side bodies with snake_case keys", async () => {
    const { fetchImpl, calls } = fakeFetch(() => okJson({ ok: true }));
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    await client.evalRun(["t1", "t2"], [{ name: "error_free" }], {
      dataset: "ds1",
      projectId: "p9",
    });
    assert.equal(calls[0].method, "POST");
    assert.equal(new URL(calls[0].url).pathname, "/v1/eval/run");
    assert.equal(calls[0].headers.get("content-type"), "application/json");
    assert.deepEqual(calls[0].body, {
      trace_ids: ["t1", "t2"],
      evaluators: [{ name: "error_free" }],
      dataset: "ds1",
      project_id: "p9",
    });
  });

  it("omits optional body fields instead of sending nulls", async () => {
    const { fetchImpl, calls } = fakeFetch(() => okJson({}));
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    await client.fleetEfficiency();
    assert.deepEqual(calls[0].body, { project_id: "default" });
    await client.fleetEfficiency(["a"]);
    assert.deepEqual(calls[1].body, {
      project_id: "default",
      trace_ids: ["a"],
    });
    await client.alertsEvaluate();
    assert.deepEqual(calls[2].body, {
      rules: [],
      use_saved: false,
      project_id: "default",
    });
    await client.compareCohorts({ trace_ids: ["a"] }, { trace_ids: ["b"] });
    assert.deepEqual(calls[3].body, {
      a: { trace_ids: ["a"] },
      b: { trace_ids: ["b"] },
      project_id: "default",
    });
    await client.runAnalysis("cost-analyst", "t/1", { runId: "r1" });
    assert.equal(
      calls[4].url,
      "http://x/v1/analyze/cost-analyst/t%2F1",
    );
    assert.deepEqual(calls[4].body, { project_id: "default", run_id: "r1" });
  });
});

describe("LensClient failures", () => {
  it("HTTP error → LensQueryError with statusCode and body detail", async () => {
    const { fetchImpl } = fakeFetch(
      () => new Response('{"detail":"trace not found"}', { status: 404 }),
    );
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    const err = await client.getTrace("missing").then(
      () => null,
      (e: unknown) => e,
    );
    assert.ok(err instanceof LensQueryError);
    assert.equal(err.statusCode, 404);
    assert.match(err.message, /GET \/traces\/missing failed: HTTP 404/);
    assert.match(err.message, /trace not found/);
  });

  it("transport error → LensQueryError without statusCode", async () => {
    const { fetchImpl } = fakeFetch(() => {
      throw new Error("connection refused");
    });
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    const err = await client.health().then(
      () => null,
      (e: unknown) => e,
    );
    assert.ok(err instanceof LensQueryError);
    assert.equal(err.statusCode, undefined);
    assert.match(err.message, /connection refused/);
  });

  it("non-JSON 200 → LensQueryError", async () => {
    const { fetchImpl } = fakeFetch(() => new Response("<html>", { status: 200 }));
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    const err = await client.metrics().then(
      () => null,
      (e: unknown) => e,
    );
    assert.ok(err instanceof LensQueryError);
    assert.match(err.message, /not JSON/);
  });

  it("times out even when the transport ignores the abort signal", async () => {
    const { fetchImpl } = fakeFetch(
      () => new Promise<Response>(() => {}), // hangs forever
    );
    const client = new LensClient({
      endpoint: "http://x",
      timeoutMs: 20,
      fetchImpl,
    });
    const err = await client.health().then(
      () => null,
      (e: unknown) => e,
    );
    assert.ok(err instanceof LensQueryError);
    assert.equal(err.statusCode, undefined);
    assert.match(err.message, /timed out after 20ms/);
  });

  it("aborts the request signal so a real fetch is cancelled", async () => {
    let sawAbort = false;
    const fetchImpl = ((url: string | URL | Request, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          sawAbort = true;
          reject(new DOMException("The operation timed out.", "TimeoutError"));
        });
      })) as typeof fetch;
    const client = new LensClient({
      endpoint: "http://x",
      timeoutMs: 10,
      fetchImpl,
    });
    await assert.rejects(() => client.health(), LensQueryError);
    assert.equal(sawAbort, true);
  });
});

describe("LensClient route coverage", () => {
  it("maps every method onto its /v1 path", async () => {
    const seen: string[] = [];
    const { fetchImpl } = fakeFetch((c) => {
      seen.push(`${c.method} ${new URL(c.url).pathname}`);
      return okJson({});
    });
    const client = new LensClient({ endpoint: "http://x", fetchImpl });
    const o = { projectId: "p" };
    await client.health();
    await client.metrics();
    await client.capabilities();
    await client.pricing();
    await client.opsSummary(o);
    await client.listTraces(o);
    await client.getTrace("t", o);
    await client.listSessions(o);
    await client.listTools(o);
    await client.traceGraph("t", o);
    await client.traceCriticalPath("t", o);
    await client.traceBottlenecks("t", o);
    await client.traceGraphAnalysis("t", o);
    await client.sessionGraph("s", o);
    await client.traceCost("t", o);
    await client.traceFailure("t", o);
    await client.traceReplay("t", o);
    await client.traceIncident("t", o);
    await client.traceSecurity("t", o);
    await client.traceEval("t", o);
    await client.traceEfficiency("t", o);
    await client.traceRecommendations("t", o);
    await client.traceLoops("t", o);
    await client.listDatasets(o);
    await client.getDataset("d", o);
    await client.listDatasetVersions("d", o);
    await client.getDatasetVersion("d", "v", o);
    await client.listSpecialists();
    await client.evalRun(["t"], [{ name: "e" }]);
    await client.evalCompare({ trace_ids: ["t"] }, { trace_ids: ["u"] }, [{ name: "e" }]);
    await client.fleetEfficiency(["t"]);
    await client.compareCohorts({ trace_ids: ["t"] }, { trace_ids: ["u"] });
    await client.runAnalysis("cost-analyst", "t");
    await client.alertsEvaluate([{ name: "r" }]);
    assert.deepEqual(seen, [
      "GET /v1/health",
      "GET /v1/metrics",
      "GET /v1/capabilities",
      "GET /v1/analytics/pricing",
      "GET /v1/ops/summary",
      "GET /v1/traces",
      "GET /v1/traces/t",
      "GET /v1/sessions",
      "GET /v1/tools",
      "GET /v1/graph/traces/t",
      "GET /v1/graph/traces/t/critical-path",
      "GET /v1/graph/traces/t/bottlenecks",
      "GET /v1/graph/traces/t/analysis",
      "GET /v1/graph/sessions/s",
      "GET /v1/traces/t/cost",
      "GET /v1/traces/t/failure",
      "GET /v1/traces/t/replay",
      "GET /v1/traces/t/incident",
      "GET /v1/traces/t/security",
      "GET /v1/traces/t/eval",
      "GET /v1/traces/t/efficiency",
      "GET /v1/traces/t/recommendations",
      "GET /v1/traces/t/loops",
      "GET /v1/datasets",
      "GET /v1/datasets/d",
      "GET /v1/datasets/d/versions",
      "GET /v1/datasets/d/versions/v",
      "GET /v1/analyze/specialists",
      "POST /v1/eval/run",
      "POST /v1/eval/compare",
      "POST /v1/intel/efficiency",
      "POST /v1/intel/regress",
      "POST /v1/analyze/cost-analyst/t",
      "POST /v1/alerts/evaluate",
    ]);
  });
});
