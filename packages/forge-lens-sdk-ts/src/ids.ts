/** W3C Trace Context ids — same semantics as the Python SDK. */

import { randomBytes } from "node:crypto";

const HEX = "0123456789abcdef";

function randomHex(bytes: number): string {
  const buf = randomBytes(bytes);
  let out = "";
  for (const b of buf) out += HEX[b >> 4] + HEX[b & 0xf];
  return out;
}

export function newTraceId(): string {
  let id = randomHex(16);
  while (id === "0".repeat(32)) id = randomHex(16);
  return id;
}

export function newSpanId(): string {
  let id = randomHex(8);
  while (id === "0".repeat(16)) id = randomHex(8);
  return id;
}

export function isValidTraceId(v: string): boolean {
  return /^[0-9a-f]{32}$/.test(v) && v !== "0".repeat(32);
}

export function isValidSpanId(v: string): boolean {
  return /^[0-9a-f]{16}$/.test(v) && v !== "0".repeat(16);
}

export interface SpanContext {
  traceId: string;
  spanId: string;
  sampled: boolean;
}

export function formatTraceparent(ctx: SpanContext): string {
  return `00-${ctx.traceId}-${ctx.spanId}-${ctx.sampled ? "01" : "00"}`;
}

export function parseTraceparent(header: string): SpanContext | null {
  if (!header) return null;
  const parts = header.trim().split("-");
  if (parts.length < 4) return null;
  const [version, traceId, spanId, flags] = parts;
  if (!/^[0-9a-f]{2}$/.test(version) || !isValidTraceId(traceId) || !isValidSpanId(spanId) || !/^[0-9a-f]{2}$/.test(flags)) {
    return null;
  }
  // W3C: sampled is bit 0x01 of the flags byte (flags=03 → sampled).
  return { traceId, spanId, sampled: (parseInt(flags, 16) & 0x01) === 1 };
}
