---
name: forge-lens-investigate-failure
description: Classify a failed trace and build an incident bundle. Use for "why did this run fail / gather failure evidence".
---
# Failure Investigation

1. `curl :7777/traces list --status error` → candidate traces.
2. `curl :7777/v1/traces/<id>/failure` → classified root-cause hypothesis
   with the error spans as evidence.
3. `curl :7777/v1/traces/<id>/incident` → exportable incident bundle.
4. `curl -XPOST :7777/v1/analyze/failure_investigator/<id> -d '{}'`.
