import fs from 'node:fs';
import path from 'node:path';

/** Shape of scripts/seed_demo_data.py's --manifest output — the contract
 *  between the seeded backend and the specs (ids are sha256-derived and
 *  stable across reseeds, but specs never hard-code them). */
export interface SeedManifest {
  project_id: string;
  db: string;
  trace_count: number;
  span_count: number;
  sessions: Record<string, { id: string; traces: number }>;
  traces: Record<string, {
    session_id: string | null;
    status: string;
    spans: number;
  }>;
  named: {
    ok_trace: string;
    rich_trace: string;
    failing_trace: string;
    slow_trace: string;
    unset_trace: string;
    events_trace: string;
    multi_tool_trace: string;
  };
  tools: string[];
  dataset: { id: string; version: string; cases: number };
  experiment: { id: string };
}

let cached: SeedManifest | null = null;

/** Read the manifest written by the API webServer's seed step. */
export function manifest(): SeedManifest {
  if (cached) return cached;
  const p = path.resolve(__dirname, '.runtime', 'seed-manifest.json');
  cached = JSON.parse(fs.readFileSync(p, 'utf-8')) as SeedManifest;
  return cached;
}
