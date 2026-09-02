-- get_snapshot_history — snapshot metadata from the Iceberg snapshots metadata table.
-- Reviewed template: every placeholder is validated before substitution.
-- Summary map values are strings in raw Iceberg metadata; cast to BIGINT so
-- the shared contract receives int | None instead of string values.
SELECT
  snapshot_id,
  parent_id AS parent_snapshot_id,
  cast(unix_timestamp(committed_at) * 1000 AS BIGINT) AS timestamp_ms,
  operation,
  cast(summary['added-data-files'] AS BIGINT) AS added_data_files,
  cast(summary['deleted-data-files'] AS BIGINT) AS removed_data_files,
  cast(summary['added-records'] AS BIGINT) AS added_records,
  cast(summary['deleted-records'] AS BIGINT) AS removed_records
FROM :table.snapshots
ORDER BY committed_at DESC
LIMIT :limit
