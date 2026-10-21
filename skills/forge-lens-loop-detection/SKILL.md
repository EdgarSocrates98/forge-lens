---
name: forge-lens-loop-detection
description: Detect repeated-identical-tool, retry and no-progress loops in a trace. Use for "is this agent stuck / looping".
---
# Loop Detection

1. `curl :7777/v1/traces/<id>/loops` → heuristic findings with span_ids.
2. Findings are `heuristic` — repetition may be legitimate polling;
   verify against `curl :7777/v1/traces/<id>/replay` before judging.
3. Threshold: `?repeat_threshold=N` (default 3).
