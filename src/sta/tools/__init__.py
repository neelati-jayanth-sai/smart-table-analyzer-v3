"""Model-facing deterministic query tools (Architecture.md #11-#13).

Each module defines one tool family: strict Pydantic parameter validation,
the backend-independent result contract, the shared rows->payload builder and
the static :class:`ToolSpec` recorded on every stored result. Tools measure
only — never diagnose, score, summarize or recommend — and SQL exists only in
reviewed templates under ``sta/execution/queries/``.
"""

from sta.tools.columns import (
    COLUMN_METADATA_METRICS_SPEC,
    ColumnMetadataMetricsParameters,
    ColumnMetadataMetricsResult,
    get_column_metadata_metrics,
)
from sta.tools.deletes import (
    DELETE_FILE_STATS_SPEC,
    DeleteFileStatsParameters,
    DeleteFileStatsResult,
    get_delete_file_stats,
)
from sta.tools.file_layout import (
    FILE_LAYOUT_SPEC,
    FileLayoutParameters,
    FileLayoutResult,
    get_file_layout,
)
from sta.tools.maintenance import (
    MAINTENANCE_CONFIG_SPEC,
    MaintenanceConfigParameters,
    MaintenanceConfigResult,
    get_iomete_maintenance_config,
)
from sta.tools.manifests import (
    MANIFEST_STATS_SPEC,
    ManifestStatsParameters,
    ManifestStatsResult,
    get_manifest_stats,
)
from sta.tools.partitions import (
    PARTITION_CANDIDATE_SPEC,
    PARTITION_LAYOUT_SPEC,
    PARTITION_SPEC_USAGE_SPEC,
    PartitionCandidateParameters,
    PartitionCandidateResult,
    PartitionLayoutParameters,
    PartitionLayoutResult,
    PartitionSpecUsageParameters,
    PartitionSpecUsageResult,
    analyze_partition_candidate,
    get_partition_layout,
    get_partition_spec_usage,
)
from sta.tools.registry import DEFAULT_REGISTRY, TOOLS
from sta.tools.sort_orders import (
    SORT_ORDER_USAGE_SPEC,
    SortOrderFileUsage,
    SortOrderUsageParameters,
    SortOrderUsageResult,
    get_sort_order_usage,
)
from sta.tools.spec import ToolSpec, payload_field_schemas, percentile
from sta.tools.table_evolution import (
    FILE_LAYOUT_HISTORY_SPEC,
    SNAPSHOT_HISTORY_SPEC,
    FileLayoutHistoryEntry,
    FileLayoutHistoryParameters,
    FileLayoutHistoryResult,
    SnapshotHistoryParameters,
    SnapshotHistoryResult,
    SnapshotSummary,
    get_file_layout_history,
    get_snapshot_history,
)

__all__ = [
    "COLUMN_METADATA_METRICS_SPEC",
    "DEFAULT_REGISTRY",
    "DELETE_FILE_STATS_SPEC",
    "FILE_LAYOUT_HISTORY_SPEC",
    "FILE_LAYOUT_SPEC",
    "MAINTENANCE_CONFIG_SPEC",
    "MANIFEST_STATS_SPEC",
    "PARTITION_CANDIDATE_SPEC",
    "PARTITION_LAYOUT_SPEC",
    "PARTITION_SPEC_USAGE_SPEC",
    "SNAPSHOT_HISTORY_SPEC",
    "SORT_ORDER_USAGE_SPEC",
    "TOOLS",
    "ColumnMetadataMetricsParameters",
    "ColumnMetadataMetricsResult",
    "DeleteFileStatsParameters",
    "DeleteFileStatsResult",
    "FileLayoutHistoryEntry",
    "FileLayoutHistoryParameters",
    "FileLayoutHistoryResult",
    "FileLayoutParameters",
    "FileLayoutResult",
    "MaintenanceConfigParameters",
    "MaintenanceConfigResult",
    "ManifestStatsParameters",
    "ManifestStatsResult",
    "PartitionCandidateParameters",
    "PartitionCandidateResult",
    "PartitionLayoutParameters",
    "PartitionLayoutResult",
    "PartitionSpecUsageParameters",
    "PartitionSpecUsageResult",
    "SnapshotHistoryParameters",
    "SnapshotHistoryResult",
    "SnapshotSummary",
    "SortOrderFileUsage",
    "SortOrderUsageParameters",
    "SortOrderUsageResult",
    "ToolSpec",
    "analyze_partition_candidate",
    "get_column_metadata_metrics",
    "get_delete_file_stats",
    "get_file_layout",
    "get_file_layout_history",
    "get_iomete_maintenance_config",
    "get_manifest_stats",
    "get_partition_layout",
    "get_partition_spec_usage",
    "get_snapshot_history",
    "get_sort_order_usage",
    "payload_field_schemas",
    "percentile",
]