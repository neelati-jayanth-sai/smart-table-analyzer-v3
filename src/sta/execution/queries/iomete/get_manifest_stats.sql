-- get_manifest_stats — manifest list at the pinned snapshot.
-- NULL-safe aggregations match the local fixture contract:
--   * a manifest with NULL entry counts contributes 0,
--   * if no manifest of the requested kind exists the result is 0,
--   * if manifests exist but none report the relevant counts the result is NULL.
-- Data manifests (content = 0) and delete manifests (content = 1) are kept
-- in separate counts so delete entries are never folded into live data files.
SELECT
  count(*) AS manifest_count,
  coalesce(sum(manifest_length), 0) AS total_manifest_size_bytes,
  CASE
    WHEN count(*) = 0 THEN 0
    WHEN count(added_files_count) + count(existing_files_count) + count(deleted_files_count) = 0 THEN NULL
    ELSE sum(coalesce(added_files_count, 0) + coalesce(existing_files_count, 0) + coalesce(deleted_files_count, 0))
  END AS total_entries,
  CASE
    WHEN sum(CASE WHEN content = 0 THEN 1 ELSE 0 END) = 0 THEN 0
    WHEN count(CASE WHEN content = 0 THEN added_files_count END) + count(CASE WHEN content = 0 THEN existing_files_count END) = 0 THEN NULL
    ELSE sum(CASE WHEN content = 0 THEN coalesce(added_files_count, 0) + coalesce(existing_files_count, 0) ELSE 0 END)
  END AS live_data_file_entries,
  CASE
    WHEN sum(CASE WHEN content = 1 THEN 1 ELSE 0 END) = 0 THEN 0
    WHEN count(CASE WHEN content = 1 THEN added_files_count END) + count(CASE WHEN content = 1 THEN existing_files_count END) = 0 THEN NULL
    ELSE sum(CASE WHEN content = 1 THEN coalesce(added_files_count, 0) + coalesce(existing_files_count, 0) ELSE 0 END)
  END AS live_delete_file_entries,
  CASE
    WHEN count(*) = 0 THEN 0
    WHEN count(deleted_files_count) = 0 THEN NULL
    ELSE sum(coalesce(deleted_files_count, 0))
  END AS deleted_entries,
  avg(manifest_length) AS avg_manifest_size_bytes,
  CASE
    WHEN count(*) = 0 THEN NULL
    WHEN count(added_files_count) + count(existing_files_count) + count(deleted_files_count) = 0 THEN NULL
    ELSE avg(coalesce(added_files_count, 0) + coalesce(existing_files_count, 0) + coalesce(deleted_files_count, 0))
  END AS avg_entries_per_manifest,
  min(manifest_length) AS min_manifest_size_bytes,
  max(manifest_length) AS max_manifest_size_bytes
FROM :table.manifests AS OF SYSTEM VERSION :snapshot_id
