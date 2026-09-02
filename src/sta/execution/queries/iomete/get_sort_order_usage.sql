-- get_sort_order_usage — live data files grouped by the sort order they were written with.
-- NULL sort_order_id means files written without a sort order.
SELECT
  sort_order_id,
  count(*) AS file_count,
  sum(file_size_in_bytes) AS total_size_bytes
FROM :table.files AS OF SYSTEM VERSION :snapshot_id
WHERE content = 0
GROUP BY sort_order_id
ORDER BY sort_order_id
