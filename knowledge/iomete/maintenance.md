---
source_type: curated
source_ref: iomete-maintenance-docs
last_verified: 2026-08-28
trust: curated
---
# IOMETE maintenance behavior

IOMETE exposes Iceberg maintenance procedures and platform-managed maintenance configuration. Maintenance answers *current data layout* problems; table-property changes answer *future writes* problems. Keep the two separate in any recommendation.

## Maintenance actions

- `rewrite_data_files` (compaction): rewrites existing data files toward a target size. This is the action that changes the layout of existing data.
- `expire_snapshots`: removes old snapshots and lets referenced metadata and files be deleted. Controls snapshot/metadata growth.
- `remove_orphan_files`: deletes files no longer referenced by any snapshot. Requires a retention window safely beyond in-flight operations.
- `rewrite_manifests`: rewrites manifest files to consolidate small manifests.

## Maintenance configuration

The effective maintenance configuration for a table (schedules, target file sizes, retention) is available through the IOMETE maintenance configuration surface. It may override generic table properties. When it is available, prefer it as evidence; when it is not available, state that explicitly instead of assuming defaults.

## Interpretation guidance

- Compaction recommendations need evidence of undersized files (file-layout measurements), not just the fact that small files exist.
- Snapshot expiration recommendations need snapshot-count and age evidence.
- A missing maintenance configuration is a limitation to report, not a guessable default.