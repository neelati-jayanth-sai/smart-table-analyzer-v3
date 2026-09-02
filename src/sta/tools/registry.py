"""Static registry of all reviewed query tools (Architecture.md #12-#13).

The registry is data, not behavior: the QueryRunner looks specs up here for
validation and versioning. Backends decide independently which of these tools
they can execute. Adding a tool means adding its module + spec here — there
is no dynamic tool discovery.
"""

from sta.tools.columns import COLUMN_METADATA_METRICS_SPEC
from sta.tools.deletes import DELETE_FILE_STATS_SPEC
from sta.tools.file_layout import FILE_LAYOUT_SPEC
from sta.tools.maintenance import MAINTENANCE_CONFIG_SPEC
from sta.tools.manifests import MANIFEST_STATS_SPEC
from sta.tools.partitions import (
    PARTITION_CANDIDATE_SPEC,
    PARTITION_LAYOUT_SPEC,
    PARTITION_SPEC_USAGE_SPEC,
)
from sta.tools.spec import ToolSpec
from sta.tools.sort_orders import SORT_ORDER_USAGE_SPEC
from sta.tools.table_evolution import FILE_LAYOUT_HISTORY_SPEC, SNAPSHOT_HISTORY_SPEC

TOOLS: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in (
        SNAPSHOT_HISTORY_SPEC,
        FILE_LAYOUT_SPEC,
        FILE_LAYOUT_HISTORY_SPEC,
        PARTITION_LAYOUT_SPEC,
        MANIFEST_STATS_SPEC,
        DELETE_FILE_STATS_SPEC,
        COLUMN_METADATA_METRICS_SPEC,
        PARTITION_CANDIDATE_SPEC,
        PARTITION_SPEC_USAGE_SPEC,
        SORT_ORDER_USAGE_SPEC,
        MAINTENANCE_CONFIG_SPEC,
    )
}

DEFAULT_REGISTRY = TOOLS

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
]