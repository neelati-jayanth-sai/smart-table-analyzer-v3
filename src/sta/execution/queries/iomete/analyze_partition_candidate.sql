-- analyze_partition_candidate — targeted aggregate profile of one selected column.
-- Identifier-like columns are rejected by the tool parameter validator before
-- this template is ever bound or executed.
--
-- Returns aggregate column facts plus deterministic distribution facts
-- (files/records per distinct value) computed with input_file_name().
WITH source_rows AS (
  SELECT :column AS value, input_file_name() AS source_file
  FROM :table AS OF SYSTEM VERSION :snapshot_id
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
)
SELECT
  sum(record_count) AS total_value_count,
  sum(CASE WHEN value IS NULL THEN record_count ELSE 0 END) AS null_count,
  count(DISTINCT value) AS distinct_count,
  cast(min(value) AS string) AS min_value,
  cast(max(value) AS string) AS max_value,
  min(file_count) AS files_per_distinct_value_min,
  percentile(file_count, 0.5) AS files_per_distinct_value_median,
  max(file_count) AS files_per_distinct_value_max,
  min(record_count) AS records_per_distinct_value_min,
  percentile(record_count, 0.5) AS records_per_distinct_value_median,
  max(record_count) AS records_per_distinct_value_max
FROM value_summary
