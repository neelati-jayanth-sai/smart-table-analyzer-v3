---
source_type: curated
source_ref: sta:diagnostics/snapshot-growth
iceberg_versions: [1.x]
spark_versions: [3.x]
engine: spark
last_verified: 2026-08-28
trust: curated
---
# Snapshot and metadata growth

Frequent commits or large metadata structures can increase planning and maintenance work.

## Investigation considerations

Compare snapshot cadence, manifest lists, and metadata sizes over an appropriate interval. Separate metadata planning cost from scan cost and validate any performance claim with scoped observations.
