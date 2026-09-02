from pydantic import BaseModel, Field
from sta.context.schema_map import ColumnInfo
from sta.context.table_metadata import PartitionSpec, SchemaField, SortOrder, TableSchema

# The full structural schema is persisted at startup as pseudo-result R000 so
# the compact context can reference it on demand (Architecture.md #8-#9).
# R000 is reserved: the ResultStore allocates R001+ per run.
FULL_SCHEMA_REF = "R000"


class TableContext(BaseModel):
    table: str
    snapshot_id: str | None = None
    format_version: int | None = None
    schema_id: int | None = None
    schema_summary: list[ColumnInfo]
    column_groups: dict[str, list[str]]
    current_partition_spec: str = "unpartitioned"
    partition_spec_id: int | None = None
    partition_spec_history_available: bool = False
    current_sort_order: str = "none"
    sort_order_id: int | None = None
    relevant_table_properties: dict[str, str] = Field(default_factory=dict)
    metrics_availability: dict[str, str] = Field(default_factory=dict)
    full_schema_ref: str | None = None
    workload_analysis: str = "disabled"


class FullSchema(BaseModel):
    """Full structural schema payload stored as R000 (Architecture.md #9:
    store full schema, reference it from the compact context). Holds complete
    field detail (docs, requiredness, field IDs), the schema history, partition
    specs and sort orders. Only curated safe properties are included; R000 can
    be read into the Investigator context on demand and must never leak
    secrets (Runtime_Environments_UI.md #6)."""

    ref: str = FULL_SCHEMA_REF
    table: str
    snapshot_id: str | None = None
    format_version: int | None = None
    schema_id: int | None = None
    fields: list[SchemaField] = Field(default_factory=list)
    schemas: list[TableSchema] = Field(default_factory=list)
    partition_specs: list[PartitionSpec] = Field(default_factory=list)
    sort_orders: list[SortOrder] = Field(default_factory=list)
    properties: dict[str, str] = Field(default_factory=dict)
