---
source_type: apache_iceberg_docs
source_ref: https://iceberg.apache.org/spec/#manifests
iceberg_versions: [1.x]
engine: spark
last_verified: 2026-08-28
trust: authoritative
---
# Manifests

Manifests describe data and delete files and their partition summaries. Manifest lists identify the manifests belonging to a snapshot.

## Investigation considerations

Inspect manifest counts, sizes, and partition-summary coverage when planning appears expensive. Separate metadata growth from data-file scan behavior and compare a suitable baseline snapshot.
