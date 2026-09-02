---
source_type: curated
source_ref: sta:diagnostics/delete-files
iceberg_versions: [1.x]
spark_versions: [3.x]
engine: spark
last_verified: 2026-08-28
trust: curated
---
# Delete-file accumulation

Accumulated row-level delete files can increase read and maintenance work, depending on delete type and workload.

## Investigation considerations

Compare delete-file counts, sizes, and associations across snapshots. Check for alternative causes such as file layout, partition skew, or query changes before attributing a regression.
