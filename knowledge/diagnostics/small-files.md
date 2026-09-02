---
source_type: curated
source_ref: sta:diagnostics/small-files
iceberg_versions: [1.x]
spark_versions: [3.x]
engine: spark
last_verified: 2026-08-28
trust: curated
---
# Small files

Many small data files may increase file-open and planning overhead, but the impact depends on workload, file format, parallelism, and metadata pruning.

## Competing explanations

Partition skew, manifest growth, delete-file work, changed predicates, and engine configuration can produce similar symptoms. Compare file-size distributions with partition and manifest observations, then seek a workload-relevant measurement. Do not infer a table fact from this note.
