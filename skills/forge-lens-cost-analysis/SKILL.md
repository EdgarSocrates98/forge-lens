---
name: forge-lens-cost-analysis
description: Token/cost/waste analysis of traces — usage coverage, priced spans, waste findings. Use for "how much did this cost / where is waste".
---
# Cost Analysis

1. `curl :7777/v1/traces/<id>/cost` → priced-span breakdown.
2. `curl :7777/v1/traces/<id>/efficiency` → tokens/tools-per-outcome.
3. `curl :7777/v1/intel/efficiency -d '{}'` → fleet roll-up.
4. Unpriced models report `reason`, never a guessed price.
