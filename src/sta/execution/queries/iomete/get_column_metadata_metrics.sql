-- get_column_metadata_metrics — aggregate per-file Iceberg metrics for one column.
-- The column field_id is resolved from the current schema before binding.
WITH per_file AS (
  SELECT
    element_at(value_counts, :field_id) AS value_count,
    element_at(null_value_counts, :field_id) AS null_count,
    element_at(nan_value_counts, :field_id) AS nan_count,
    element_at(lower_bounds, :field_id) AS lower_bound,
    element_at(upper_bounds, :field_id) AS upper_bound
  FROM :table.files AS OF SYSTEM VERSION :snapshot_id
  WHERE content = 0
)
SELECT
  :column AS column,
  :field_id AS field_id,
  count(*) AS files_measured,
  sum(CASE WHEN value_count IS NOT NULL THEN 1 ELSE 0 END) AS files_with_value_counts,
  sum(CASE WHEN lower_bound IS NOT NULL THEN 1 ELSE 0 END) AS files_with_bounds,
  sum(value_count) AS value_count_sum,
  sum(null_count) AS null_value_count_sum,
  sum(nan_count) AS nan_value_count_sum,
  min(lower_bound) AS lower_bound,
  max(upper_bound) AS upper_bound
FROM per_file
