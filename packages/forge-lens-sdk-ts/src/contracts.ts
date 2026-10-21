/** Contract v1 types — mirrors schemas/v1/*.json (single source: Python models). */

export const SCHEMA_VERSION = "1.0.0" as const;

export type Provenance = "observed" | "imported" | "derived" | "inferred" | "unknown";

export type StatusCode = "unset" | "ok" | "error";

export interface Status {
  code: StatusCode;
  message?: string | null;
}

export type SpanKind =
  | "internal" | "server" | "client" | "producer" | "consumer"
  | "agent" | "chain" | "llm" | "tool" | "retriever" | "embedding"
  | "reranker" | "guardrail" | "evaluator" | "other";

export interface SpanEvent {
  name: string;
  time: string; // ISO-8601
  attributes?: Record<string, unknown>;
}

export interface SpanLink {
  trace_id: string;
  span_id: string;
  attributes?: Record<string, unknown>;
}

export interface Span {
  schema_version: typeof SCHEMA_VERSION;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  name: string;
  kind: SpanKind;
  start_time: string;
  end_time: string | null;
  attributes: Record<string, unknown>;
  events: SpanEvent[];
  links: SpanLink[];
  status: Status;
  provenance: Provenance;
  integrity_flags: string[];
}

export interface TraceSource {
  system: string;
  sdk_name?: string | null;
  sdk_version?: string | null;
  host?: string | null;
  runtime?: string | null;
  language?: string | null;
}

export interface Trace {
  schema_version: typeof SCHEMA_VERSION;
  trace_id: string;
  project_id: string;
  session_id: string | null;
  root_span_id: string | null;
  started_at: string;
  ended_at: string | null;
  status: Status;
  source: TraceSource;
  resource_attributes: Record<string, unknown>;
  spans: Span[];
  provenance: Provenance;
}

export interface TraceIngest {
  schema_version: typeof SCHEMA_VERSION;
  project_id: string;
  source?: TraceSource | null;
  traces: Trace[];
  sent_at?: string | null;
}
