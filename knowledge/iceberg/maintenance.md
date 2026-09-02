---
source_type: apache_iceberg_docs
source_ref: https://iceberg.apache.org/docs/latest/maintenance/
iceberg_versions: [1.x]
engine: spark
last_verified: 2026-08-28
trust: authoritative
---
# Maintenance

Rewrite, expiration, and orphan-file procedures change table metadata or physical layout. Availability and retention policies determine which operations are appropriate.

## Investigation considerations

Use maintenance history as context and verify its effect with snapshot-scoped observations. Compaction can address file layout but should not be assumed to be the remedy for every latency symptom.
