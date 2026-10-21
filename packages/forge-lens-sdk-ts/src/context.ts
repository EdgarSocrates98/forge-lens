/** Async context propagation via AsyncLocalStorage (Node) with a sync fallback. */

import { AsyncLocalStorage } from "node:async_hooks";
import type { SpanContext } from "./ids.js";

const als = new AsyncLocalStorage<SpanContext[]>();

/** Current span context, if inside a `LensTracer.span()`/`withSpan` callback. */
export function currentSpan(): SpanContext | undefined {
  const stack = als.getStore();
  return stack && stack.length > 0 ? stack[stack.length - 1] : undefined;
}

/** Run `fn` with `ctx` pushed as current span. */
export function withSpan<T>(ctx: SpanContext, fn: () => T): T {
  const stack = als.getStore() ?? [];
  return als.run([...stack, ctx], fn);
}
