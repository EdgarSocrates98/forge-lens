---
name: forge-lens-graph-explorer
description: Explore execution/session graphs — critical path, centrality, concentration. Use for "show me the execution structure / what was the critical path".
---
# Graph Explorer

1. `curl :7777/v1/graph/traces/<id>` → node/edge projection.
2. `curl :7777/v1/graph/traces/<id>/analysis` → centrality,
   concentration, failure propagation, repeated subgraphs.
3. `curl :7777/v1/graph/sessions/<id>` → cross-trace session graph.
4. Dangling edges are evidence (missing parents), not bugs.
