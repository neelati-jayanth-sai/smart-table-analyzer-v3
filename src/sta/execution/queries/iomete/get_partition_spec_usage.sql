-- get_partition_spec_usage — live data files grouped by the partition spec they were written under.
-- The backend enriches each row with the rendered spec fields from metadata.
SELECT
  spec_id,
  count(*) AS file_count,
  sum(file_size_in_bytes) AS total_size_bytes
FROM :table.files AS OF SYSTEM VERSION :snapshot_id
WHERE content = 0
GROUP BY spec_id
ORDER BY spec_id
