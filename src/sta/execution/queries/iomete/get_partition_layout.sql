-- get_partition_layout — physical partitions at the pinned snapshot.
-- The partition struct is normalized into a Python dict by the production
-- connection adapter; the contract expects dict[str, str].
-- NOTE: no LIMIT here. The shared tool contract computes distribution
-- statistics (min/max/median files and bytes per partition, plus
-- largest/smallest partition) over the full population and only bounds the
-- returned `entries` list.
SELECT
  partition,
  spec_id,
  count(*) AS file_count,
  sum(file_size_in_bytes) AS total_size_bytes,
  sum(record_count) AS total_record_count
FROM :table.files AS OF SYSTEM VERSION :snapshot_id
WHERE content = 0
GROUP BY partition, spec_id
ORDER BY total_size_bytes DESC
