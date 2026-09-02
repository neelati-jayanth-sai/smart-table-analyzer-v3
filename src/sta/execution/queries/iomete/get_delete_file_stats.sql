-- get_delete_file_stats — delete files at the pinned snapshot.
-- NULL-safe: total_delete_records stays NULL when no delete file reports a
-- record count, matching the local sum_optional contract (no invented zeros).
WITH deletes AS (
  SELECT file_size_in_bytes, record_count, content
  FROM :table.files AS OF SYSTEM VERSION :snapshot_id
  WHERE content != 0
)
SELECT
  count(*) AS delete_file_count,
  sum(CASE WHEN content = 1 THEN 1 ELSE 0 END) AS position_delete_file_count,
  sum(CASE WHEN content = 2 THEN 1 ELSE 0 END) AS equality_delete_file_count,
  coalesce(sum(file_size_in_bytes), 0) AS total_delete_file_size_bytes,
  CASE WHEN count(record_count) > 0 THEN sum(record_count) END AS total_delete_records,
  min(file_size_in_bytes) AS min_delete_file_size_bytes,
  percentile(file_size_in_bytes, 0.5) AS median_delete_file_size_bytes,
  max(file_size_in_bytes) AS max_delete_file_size_bytes
FROM deletes
