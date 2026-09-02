---
source_type: team_runbook
source_ref: data-platform-runbook
last_verified: 2026-08-28
trust: curated
---
# File sizing runbook

Preferred data-file range: **256–512 MiB**.

## Before recommending compaction

Confirm with file-layout measurements that the table actually deviates:

- median and p90 file size materially below 256 MiB, and
- file count high enough that planning/scan overhead is plausible, and
- the small-file pattern is broad, not one partition or one bad write.

Compaction rewrites existing files; recommend it as **immediate remediation** only when the measurements justify the cost.

## Writer configuration

When undersized files recur, check the writer configuration that shapes *future* files (`write.target-file-size-bytes`, distribution mode, commit frequency). Configuration changes do not rewrite existing files — pair them with compaction when both are needed.

## For streaming tables

Streaming and frequent micro-batch commits accumulate small files and snapshots by design. For these tables prefer:

- compaction on a schedule instead of one-off fixes, and
- snapshot expiration on a schedule to contain metadata growth.

A streaming table with modest small files may be operating as designed; say so rather than forcing a rewrite recommendation.

## Exceptions

- Tables small in absolute terms (few hundred files) may not justify any action.
- Tables at end of life with no ongoing writes gain little from writer changes.
- If partitioning itself causes the fragmentation (high fan-out), fixing the partition spec is the future-design answer; compaction alone treats the symptom.