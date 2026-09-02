---
source_type: apache_iceberg_docs
source_ref: https://iceberg.apache.org/spec/#delete-files
iceberg_versions: [1.x]
engine: spark
last_verified: 2026-08-28
trust: authoritative
---
# Delete files

Delete files represent row-level deletions and are associated with data files or partitions. Their presence can add work during reads and maintenance.

## Investigation considerations

Inspect delete-file type, count, size, and association with data files at the relevant snapshot. Consider write patterns and compaction history, and distinguish metadata observations from measured query impact.
