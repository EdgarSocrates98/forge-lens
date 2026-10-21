---
name: forge-lens-trace-analysis
description: Analyze a stored agent trace — spans, errors, timeline, evidence. Use when asked "what did this agent do / why is it slow".
---
# Trace Analysis

1. `forge-lens traces list --limit 20` → pick the trace_id.
2. `forge-lens traces show <id>` → span timeline with status/durations.
3. `curl :7777/v1/traces/<id>/replay` → deterministic event replay.
4. Report only observed evidence; cite span_ids. Never infer intent.
