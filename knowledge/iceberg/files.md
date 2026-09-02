---
source_type: apache_iceberg_docs
source_ref: https://iceberg.apache.org/docs/latest/spark-procedures/
iceberg_versions: [1.x]
engine: spark
last_verified: 2026-08-28
trust: authoritative
---
# Data files

Data files are the physical files referenced by Iceberg metadata. File count and size distributions can affect planning and scan overhead, while compression, row-group layout, and predicate selectivity affect read cost.

## Investigation considerations

Inspect file counts, sizes, formats, and partition association at the pinned snapshot. Distinguish a broad small-file pattern from a concentrated partition or workload effect; neither pattern is by itself proof of a regression.
