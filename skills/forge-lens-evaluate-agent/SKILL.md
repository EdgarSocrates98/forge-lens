---
name: forge-lens-evaluate-agent
description: Run deterministic evaluators and A/B comparisons over traces/datasets. Use for "is the new version better / run the eval".
---
# Evaluate Agent

1. `curl -XPOST :7777/v1/eval/run -d '{"trace_ids":[...],"evaluators":[{"name":"error_free"}]}'`.
2. `curl -XPOST :7777/v1/eval/compare -d '{"a":{...},"b":{...},"evaluators":[...],"gate":{...}}'`
   → observed deltas + significance where computable + gate verdict.
3. Versioned datasets: `/v1/datasets*` CRUD first, then `dataset` refs.
