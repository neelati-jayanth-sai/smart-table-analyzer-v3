---
source_type: curated
source_ref: iomete-platform-docs
last_verified: 2026-08-28
trust: curated
---
# IOMETE platform overview

IOMETE runs Apache Iceberg tables on managed Spark. The platform owns the catalog, query execution, and maintenance surfaces; tables are accessed through qualified catalog names and measured through Iceberg metadata tables and platform maintenance configuration.

## Execution behavior

- Reads and writes go through Spark with Iceberg extensions enabled; distribution and write parallelism follow Iceberg write distribution-mode settings combined with platform defaults.
- Iceberg metadata tables (snapshots, files, manifests, partitions) are available per table and are the preferred source for table measurements.
- Platform feature support can differ from stock open-source behavior; when a feature is platform-specific, treat platform documentation as authoritative over generic Iceberg guidance.

## Property precedence

Configuration is layered. Effective configuration can come from, in increasing precedence: Iceberg table properties, IOMETE maintenance/domain overrides, and job-level settings. When advising on table properties, prefer effective configuration evidence when it is available, and state clearly when only declared table properties were visible.

Iceberg property keys are case-sensitive: engines recognize only the exact lowercase writer keys (e.g. `write.target-file-size-bytes`). A key stored with different casing (e.g. `WRITE.TARGET-FILE-SIZE-BYTES`) is preserved as inert custom table metadata and configures nothing, even when its value looks like a standard writer setting. A table can therefore carry an uppercase key while the effective lowercase key is absent; treat the declared value and the effective configuration as separate facts and verify with measurements.

## Constraints to remember

- Changing table metadata (partition spec, sort order, properties) affects future writes only; existing data files keep their current layout until rewritten.
- Maintenance behavior (compaction, snapshot expiration, orphan cleanup) may be governed by platform schedules or overrides rather than table properties; check the maintenance configuration before assuming a default.
- Some Iceberg features are gated by platform version. If a capability cannot be confirmed for the platform, say so instead of assuming it.