---
source_type: apache_iceberg_docs
source_ref: https://iceberg.apache.org/spec/
iceberg_versions: [1.x]
engine: spark
last_verified: 2026-08-28
trust: authoritative
---
# Snapshots

An Iceberg snapshot identifies a consistent table state. Snapshot history can help locate when behavior changed, but history alone does not establish a cause.

## Investigation considerations

Compare relevant snapshots and inspect the metadata associated with each. Consider concurrent writes, schema or partition evolution, and maintenance operations as alternative explanations. Pin observations to an explicit snapshot before treating them as comparable.
