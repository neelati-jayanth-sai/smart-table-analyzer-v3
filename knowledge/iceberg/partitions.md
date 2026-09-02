---
source_type: apache_iceberg_docs
source_ref: https://iceberg.apache.org/docs/latest/partitioning/
iceberg_versions: [1.x]
engine: spark
last_verified: 2026-08-28
trust: authoritative
---
# Partitions

Partition transforms determine how records map to partition values. Partition evolution can leave files written under multiple layouts.

## Investigation considerations

Inspect partition distributions and file sizes together. A small number of highly populated partitions can create skew even when overall file statistics look healthy. Account for partition evolution before comparing groups.
