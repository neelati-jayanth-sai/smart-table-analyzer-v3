---
source_type: team_runbook
source_ref: data-platform-runbook
last_verified: 2026-08-28
trust: curated
---
# Maintenance runbook

Team conventions for Iceberg table maintenance on IOMETE.

## Conventions

- Data-file compaction targets the file-sizing runbook range (256–512 MiB). Do not schedule compaction without checking whether small files are actually material for the table.
- Snapshot expiration: retain at least 7 days of snapshot history; longer for tables that support time-travel-driven consumers.
- Orphan-file removal: retention window of at least 3 days; never run against tables with long-running writes without confirming the window.
- Manifest rewrites: only when manifest counts or small-manifest measurements show real overhead.

## Scheduling

- Streaming/high-frequency tables get scheduled maintenance (compaction + snapshot expiration).
- Batch tables are reviewed on measurement evidence, not on a fixed calendar.

## Separation of concerns

- Maintenance (rewrites, expiration) repairs *current* layout and metadata.
- Table-property and spec changes prevent *future* recurrence.
- Every maintenance recommendation should say which of the two it is, and property recommendations must not be presented as fixing existing files.

## Property defaults

Preferred defaults unless platform-effective configuration says otherwise: `write.target-file-size-bytes` sized so typical output files land in the preferred range; snapshot retention per the conventions above. When IOMETE maintenance configuration is available, it wins over these defaults.