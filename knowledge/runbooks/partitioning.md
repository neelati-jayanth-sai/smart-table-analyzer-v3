---
source_type: team_runbook
source_ref: data-platform-runbook
last_verified: 2026-08-28
trust: curated
---
# Partition design runbook

Team preferences for Iceberg partition specs. Recommendations must be expressed as Iceberg transforms (e.g. `days(ts)`, `bucket(32, id)`, `truncate(8, code)`), not bare column names.

## Preferences

- Prefer temporal transforms (`hours`, `days`, `months`) on the event timestamp when query patterns filter on time. Choose granularity from data volume: `hours` only for very high daily volume, `days` as the default, `months` for low-volume tables.
- Prefer `bucket(n, col)` over `identity` for high-cardinality identifier-like columns when partition pruning on that column is genuinely required; identifiers as identity partitions fragment the table.
- Avoid identity partitioning on columns with millions of distinct values.
- `unpartitioned` is a valid recommendation for small tables where partitioning only adds file fragmentation.

## Partition evolution caveats

- Changing the partition spec is metadata-only: new writes use the new spec, existing files keep their old spec.
- Rewriting historical data to the new spec is a separate, costed action. State the distinction explicitly in every partition recommendation.
- Multiple specs coexist after evolution; measurements must be read per spec.

## Evidence expectations

Before recommending a new spec, expect evidence from: current partition spec, partition distribution, file layout by partition, partition evolution, and column metadata metrics. One targeted candidate analysis is acceptable when it is justified; never profile all columns.