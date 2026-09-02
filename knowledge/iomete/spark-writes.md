---
source_type: curated
source_ref: iomete-spark-iceberg-writes
last_verified: 2026-08-28
trust: curated
---
# Spark writes to Iceberg on IOMETE

Write behavior determines future file layout. These notes describe how Spark/Iceberg writes translate into file layout and metadata, so Investigators can connect configuration to observed measurements.

## File-size drivers

- `write.target-file-size-bytes` sets the intended data-file size. Writers aim near the target but do not guarantee it; small input batches, high fan-out partitioning, and frequent commits all produce smaller files.
- Row-group and page layout (`write.parquet.row-group-size-bytes`, page-size properties) affect scan efficiency independently of file size.

## Distribution and fan-out

- `write.distribution-mode` (`none`, `hash`, `range`) controls how rows are clustered before writing.
- High partition fan-out (many partitions per write) fragments output across partitions and tends to create small files even with a correct target size.

## Sort orders and writes

- A table sort order guides how data is clustered within files at write time. Sorting adds write cost; without workload evidence the benefit is speculative.
- Defining a sort order does not reorder existing files; only rewrites do.

## Snapshot behavior

- Every write produces a new snapshot; streaming or micro-batch patterns accumulate snapshots and metadata quickly unless snapshot expiration is scheduled.