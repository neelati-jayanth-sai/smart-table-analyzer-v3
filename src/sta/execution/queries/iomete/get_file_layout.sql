-- get_file_layout — aggregate the data files at the pinned snapshot.
-- Empty selections preserve file_count=0 / total_size_bytes=0; distribution
-- fields are NULL and normalize to None in the shared contract.
WITH data_files AS (
  SELECT file_size_in_bytes, record_count
  FROM :table.files AS OF SYSTEM VERSION :snapshot_id
  WHERE content = 0
)
SELECT
  count(*) AS file_count,
  coalesce(sum(file_size_in_bytes), 0) AS total_size_bytes,
  sum(record_count) AS total_record_count,
  min(file_size_in_bytes) AS min_file_size_bytes,
  max(file_size_in_bytes) AS max_file_size_bytes,
  avg(file_size_in_bytes) AS avg_file_size_bytes,
  percentile(file_size_in_bytes, 0.5) AS median_file_size_bytes,
  percentile(file_size_in_bytes, 0.25) AS p25_file_size_bytes,
  percentile(file_size_in_bytes, 0.9) AS p90_file_size_bytes,
  percentile(file_size_in_bytes, 0.95) AS p95_file_size_bytes,
  min(record_count) AS min_record_count,
  max(record_count) AS max_record_count,
  percentile(record_count, 0.5) AS median_record_count
FROM data_files
