---
name: forge-lens-context-analysis
description: Context-window pressure and input-token growth across LLM spans. Use for "is the context ballooning / will it overflow".
---
# Context Analysis

1. `curl -XPOST :7777/v1/analyze/context_efficiency_analyst/<id> -d '{}'`
   → cumulative input tokens + growth per trace.
2. `curl :7777/v1/traces/<id>/recommendations` → context_budget
   candidates (>=100k-token calls flagged).
3. Usage absent → coverage says so; never fabricate token counts.
