-- get_file_layout_history — cumulative layout totals recorded in snapshot summaries.
-- Raw Iceberg summary map values are strings; cast to BIGINT so the shared
-- contract receives int | None instead of string values.
SELECT
  snapshot_id,
  cast(unix_timestamp(committed_at) * 1000 AS BIGINT) AS timestamp_ms,
  operation,
  cast(summary['total-data-files'] AS BIGINT) AS total_data_files,
  cast(summary['total-files-size'] AS BIGINT) AS total_data_size_bytes,
  cast(summary['total-records'] AS BIGINT) AS total_records,
  cast(summary['added-data-files'] AS BIGINT) AS added_data_files,
  cast(summary['deleted-data-files'] AS BIGINT) AS removed_data_files
FROM :table.snapshots
ORDER BY committed_at DESC
LIMIT :limit
