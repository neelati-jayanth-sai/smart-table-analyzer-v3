---
source_type: curated
source_ref: sta:diagnostics/partition-skew
iceberg_versions: [1.x]
spark_versions: [3.x]
engine: spark
last_verified: 2026-08-28
trust: curated
---
# Partition skew

Uneven records, bytes, or files across partitions can create stragglers and reduce effective parallelism.

## Competing explanations

Small files, data hot spots, changed partition transforms, and query predicate changes can resemble skew. Inspect partition-level distributions and compare them with the workload and a prior snapshot; this note is guidance, not current-table evidence.
