"""TableContext and schema compression (Architecture.md #8-#10, #15-#16).

Startup builds a compact, metadata-derived TableContext plus the R000
full-schema record from backend-independent TableMetadata supplied through the
TableMetadataProvider seam. No DDL, no PyIceberg imports, no execution here.
"""

from sta.context.context_builder import (
    RELEVANT_PROPERTY_KEYS,
    StartupContext,
    build_startup_context,
    describe_metrics_availability,
    relevant_table_properties,
    render_transform,
    resolve_metrics_availability,
)
from sta.context.schema_map import (
    ColumnInfo,
    classify_column,
    group_columns,
    is_identifier_like,
)
from sta.context.table_context import FULL_SCHEMA_REF, FullSchema, TableContext
from sta.context.table_metadata import (
    PartitionField,
    PartitionSpec,
    SchemaField,
    SortField,
    SortOrder,
    StaticMetadataProvider,
    TableMetadata,
    TableMetadataProvider,
    TableSchema,
    table_metadata_from_dict,
)

__all__ = [
    "FULL_SCHEMA_REF",
    "FullSchema",
    "PartitionField",
    "PartitionSpec",
    "RELEVANT_PROPERTY_KEYS",
    "SchemaField",
    "SortField",
    "SortOrder",
    "StaticMetadataProvider",
    "StartupContext",
    "TableContext",
    "TableMetadata",
    "TableMetadataProvider",
    "TableSchema",
    "build_startup_context",
    "classify_column",
    "describe_metrics_availability",
    "group_columns",
    "is_identifier_like",
    "relevant_table_properties",
    "render_transform",
    "resolve_metrics_availability",
    "table_metadata_from_dict",
]