-- analyze_partition_candidate — local DuckDB template for targeted column profiling.
-- Produces aggregate column facts plus deterministic distribution facts
-- (files/records per distinct value) from a single scan of the pinned
-- snapshot's data files. The :source placeholder is built internally from the
-- resolved table scan path; it is never derived from model input.
WITH source_rows AS (
  SELECT :column AS value, filename AS source_file
  FROM :source
),
per_value_file AS (
  SELECT
    value,
    source_file,
    count(*) AS row_count
  FROM source_rows
  GROUP BY value, source_file
),
value_summary AS (
  SELECT
    value,
    sum(row_count) AS record_count,
    count(DISTINCT source_file) AS file_count
  FROM per_value_file
  GROUP BY value
),
top_values AS (
  SELECT
    list(
      struct_pack(
        value := cast(value AS varchar),
        file_count := file_count,
        record_count := record_count
      )
    ) AS top_values
  FROM (
    SELECT value, file_count, record_count
    FROM value_summary
    ORDER BY record_count DESC
    LIMIT 25
  )
)
SELECT
  sum(record_count) AS total_value_count,
  sum(CASE WHEN value IS NULL THEN record_count ELSE 0 END) AS null_count,
  count(DISTINCT value) AS distinct_count,
  cast(min(value) AS varchar) AS min_value,
  cast(max(value) AS varchar) AS max_value,
  min(file_count) AS files_per_distinct_value_min,
  cast(percentile_cont(0.5) WITHIN GROUP (ORDER BY file_count) AS double) AS files_per_distinct_value_median,
  max(file_count) AS files_per_distinct_value_max,
  min(record_count) AS records_per_distinct_value_min,
  cast(percentile_cont(0.5) WITHIN GROUP (ORDER BY record_count) AS double) AS records_per_distinct_value_median,
  max(record_count) AS records_per_distinct_value_max,
  coalesce((SELECT top_values FROM top_values), []) AS top_values
FROM value_summary
