"""Compact startup context builder (Architecture.md #8-#10).

Converts explicit, backend-independent :class:`TableMetadata` into:

- the compact ``TableContext`` given to the Investigator before its first turn,
  preserving schema_id, field IDs, partition/sort IDs, snapshot, format
  version, grouped structural schema, relevant safe properties, metrics
  availability and the R000 full-schema reference;
- the :class:`FullSchema` record persisted as pseudo-result R000.

The builder is a pure deterministic function: it never parses raw DDL, never
imports PyIceberg, and never executes queries (Runtime_Environments_UI.md #10:
PyIceberg only supplies Iceberg facts through the metadata provider seam).
Structural classification and identifier policy come exclusively from the
existing ``sta.context.schema_map`` module.
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from sta.context.schema_map import ColumnInfo, classify_column, group_columns
from sta.context.table_context import FULL_SCHEMA_REF, FullSchema, TableContext
from sta.context.table_metadata import (
    PartitionSpec,
    SchemaField,
    SortField,
    SortOrder,
    TableMetadata,
    TableSchema,
    table_metadata_from_dict,
)

UNPARTITIONED = "unpartitioned"
NO_SORT_ORDER = "none"
UNKNOWN_SPEC = "unknown"

# Iceberg's default metrics mode when the property is absent (Iceberg spec).
DEFAULT_METRICS_MODE = "truncate(16)"

# Iceberg metadata-JSON keys that resolve metrics availability per column.
_METRICS_DEFAULT_PROPERTY = "write.metadata.metrics.default"
_METRICS_COLUMN_PREFIX = "write.metadata.metrics.column."

# Curated allowlist of safe, analysis-relevant table properties. Unknown
# properties are dropped from the compact context so nothing unexpected
# (including credential-like configuration) can reach the model.
RELEVANT_PROPERTY_KEYS = frozenset(
    {
        "write.format.default",
        "write.distribution-mode",
        "write.target-file-size-bytes",
        "write.parquet.row-group-size-bytes",
        "write.delete.mode",
        "write.update.mode",
        "write.merge.mode",
        "write.metadata.metrics.default",
        "write.metadata.previous-versions-max",
        "history.expire.max-snapshot-age-ms",
        "history.expire.min-snapshots-to-keep",
    }
)

# Lowercase index over the allowlist so known Iceberg property keys written
# with non-canonical casing (e.g. ``WRITE.TARGET-FILE-SIZE-BYTES``) stay
# visible. Iceberg property keys are case-sensitive, so such a variant is
# inert: it is kept verbatim (never normalized to the canonical lowercase
# key) so the Investigator can notice it without mistaking it for the
# effective writer configuration.
_RELEVANT_PROPERTY_KEYS_BY_LOWER = {key.lower(): key for key in RELEVANT_PROPERTY_KEYS}

_TEMPORAL_TRANSFORMS = {"year": "years", "month": "months", "day": "days", "hour": "hours"}


class StartupContext(BaseModel):
    """Everything the run startup produces from metadata: the compact context
    for the Investigator plus the R000 full-schema record for the store."""

    table_context: TableContext
    full_schema: FullSchema


def build_startup_context(metadata: TableMetadata | Mapping[str, Any]) -> StartupContext:
    """Build the startup context from explicit Iceberg-like metadata.

    Accepts a :class:`TableMetadata` or a fixture dict (Iceberg metadata-JSON
    kebab-case or snake_case, with a ``table`` key). Raises ``ValueError`` on
    internally inconsistent metadata (e.g. a current-schema/spec/sort id that
    the supplied lists do not contain) rather than guessing.
    """
    md = metadata if isinstance(metadata, TableMetadata) else table_metadata_from_dict(metadata)
    schema = _resolve_current_schema(md)
    fields_by_id = {field.field_id: field.name for field in schema.fields}

    schema_summary = [
        ColumnInfo(name=field.name, type=field.type, field_id=field.field_id)
        for field in schema.fields
    ]

    current_partition_spec, partition_spec_history_available = _resolve_partition_spec(
        md, fields_by_id
    )
    current_sort_order = _resolve_sort_order(md, fields_by_id)
    snapshot_id = None if md.current_snapshot_id is None else str(md.current_snapshot_id)
    properties = relevant_table_properties(md.properties)

    table_context = TableContext(
        table=md.table,
        snapshot_id=snapshot_id,
        format_version=md.format_version,
        schema_id=schema.schema_id,
        schema_summary=schema_summary,
        column_groups=group_columns(schema_summary),
        current_partition_spec=current_partition_spec,
        partition_spec_id=md.default_spec_id,
        partition_spec_history_available=partition_spec_history_available,
        current_sort_order=current_sort_order,
        sort_order_id=md.default_sort_order_id,
        relevant_table_properties=properties,
        metrics_availability=resolve_metrics_availability(
            schema.fields, md.properties, md.metrics_availability
        ),
        full_schema_ref=FULL_SCHEMA_REF,
        workload_analysis="disabled",
    )
    full_schema = FullSchema(
        table=md.table,
        snapshot_id=snapshot_id,
        format_version=md.format_version,
        schema_id=schema.schema_id,
        fields=list(schema.fields),
        schemas=list(md.schemas),
        partition_specs=list(md.partition_specs) if md.partition_specs is not None else [],
        sort_orders=list(md.sort_orders) if md.sort_orders is not None else [],
        properties=properties,
    )
    return StartupContext(table_context=table_context, full_schema=full_schema)


def _resolve_current_schema(md: TableMetadata) -> TableSchema:
    for schema in md.schemas:
        if schema.schema_id == md.current_schema_id:
            return schema
    raise ValueError(
        f"Table {md.table!r}: current_schema_id {md.current_schema_id} "
        f"not found in {len(md.schemas)} supplied schema(s)"
    )


def _resolve_partition_spec(
    md: TableMetadata, fields_by_id: Mapping[int, str]
) -> tuple[str, bool]:
    """Return (rendered current spec, spec history available).

    A supplied partition-specs list models the full Iceberg spec history, so
    history is available only when more than one spec is present. When no list
    is supplied at all, a preserved default-spec-id still shows that the table
    is partitioned, rendered as ``unknown``.
    """
    if md.partition_specs is None:
        spec = UNPARTITIONED if md.default_spec_id is None else UNKNOWN_SPEC
        return spec, False
    history_available = len(md.partition_specs) > 1
    current = _find_partition_spec(md.partition_specs, md.default_spec_id, md.table)
    if current is None or not current.fields:
        return UNPARTITIONED, history_available
    rendered = ", ".join(
        render_transform(field.transform, fields_by_id.get(field.source_id, field.name))
        for field in current.fields
    )
    return rendered, history_available


def _resolve_sort_order(
    md: TableMetadata, fields_by_id: Mapping[int, str]
) -> str:
    if md.sort_orders is None or md.default_sort_order_id is None:
        return NO_SORT_ORDER
    current = _find_sort_order(md.sort_orders, md.default_sort_order_id, md.table)
    if current is None or not current.fields:
        return NO_SORT_ORDER
    return ", ".join(
        render_sort_field(field, fields_by_id) for field in current.fields
    )


def _find_partition_spec(
    specs: list[PartitionSpec], spec_id: int | None, table: str
) -> PartitionSpec | None:
    if spec_id is None:
        return None
    for spec in specs:
        if spec.spec_id == spec_id:
            return spec
    raise ValueError(
        f"Table {table!r}: partition spec id {spec_id} not found in the supplied metadata"
    )


def _find_sort_order(
    orders: list[SortOrder], order_id: int | None, table: str
) -> SortOrder | None:
    if order_id is None:
        return None
    for order in orders:
        if order.order_id == order_id:
            return order
    raise ValueError(
        f"Table {table!r}: sort order id {order_id} not found in the supplied metadata"
    )


def render_transform(transform: str, column: str) -> str:
    """Render one Iceberg transform compactly, e.g. ``day`` -> ``days(created_at)``."""
    t = transform.strip().lower()
    if t == "identity":
        return column
    if t in _TEMPORAL_TRANSFORMS:
        return f"{_TEMPORAL_TRANSFORMS[t]}({column})"
    if t == "void":
        return "void"
    return f"{transform.strip()}({column})"


def render_sort_field(field: SortField, fields_by_id: Mapping[int, str]) -> str:
    column = fields_by_id.get(field.source_id, f"<field:{field.source_id}>")
    expression = render_transform(field.transform, column)
    direction = field.direction.strip().upper()
    null_order = field.null_order.strip().upper().replace("-", " ").replace("_", " ")
    return f"{expression} {direction} {null_order}"


def relevant_table_properties(properties: Mapping[str, str]) -> dict[str, str]:
    """Filter table properties down to the curated safe/relevant allowlist,
    sorted by key for deterministic output.

    A known key is recognized case-insensitively, but its raw casing is
    preserved: Iceberg property keys are case-sensitive, so a case variant
    such as ``WRITE.TARGET-FILE-SIZE-BYTES`` is inert custom metadata, and
    the context must keep it distinguishable from the effective lowercase
    key. Case variants stay separate entries and never overwrite each other
    or the canonical lowercase key; only exact lowercase keys feed derived
    facts such as metrics availability."""
    return {
        key: properties[key]
        for key in sorted(properties)
        if key.lower() in _RELEVANT_PROPERTY_KEYS_BY_LOWER
    }


def resolve_metrics_availability(
    fields: list[SchemaField],
    properties: Mapping[str, str],
    explicit: Mapping[str, str] | None,
) -> dict[str, str]:
    """Per-column metrics availability for the current schema.

    A backend-supplied ``explicit`` map (measured availability) is used
    verbatim for the columns it names. Otherwise availability is derived
    deterministically from the Iceberg metrics configuration: the per-column
    override (``write.metadata.metrics.column.<field-id>`` or ``.<name>``)
    wins over ``write.metadata.metrics.default``.
    """
    if explicit is not None:
        return {field.name: explicit[field.name] for field in fields if field.name in explicit}
    default_mode = properties.get(_METRICS_DEFAULT_PROPERTY, DEFAULT_METRICS_MODE)
    overrides = {
        key[len(_METRICS_COLUMN_PREFIX):]: value
        for key, value in properties.items()
        if key.startswith(_METRICS_COLUMN_PREFIX)
    }
    availability: dict[str, str] = {}
    for field in fields:
        mode = overrides.get(str(field.field_id), overrides.get(field.name, default_mode))
        availability[field.name] = describe_metrics_availability(mode, field.type)
    return availability


def describe_metrics_availability(mode: str, column_type: str) -> str:
    """Map an Iceberg metrics mode + column type to a compact availability
    string. Missing bounds/counts do not mean missing data (Architecture.md
    #16): the value describes configured availability, never data presence.
    Unknown modes are passed through unchanged."""
    mode = mode.strip()
    if mode == "none":
        return "none"
    if mode == "counts":
        return "counts only"
    if mode.startswith("truncate"):
        return "truncated bounds + counts available"
    if mode == "integers":
        return (
            "bounds + counts available"
            if _has_int_bounds(column_type)
            else "counts only"
        )
    if mode == "floats":
        return (
            "bounds + counts available"
            if _has_float_bounds(column_type)
            else "counts only"
        )
    return mode


def _has_int_bounds(column_type: str) -> bool:
    """Integers mode collects bounds for numeric and temporal columns; the
    structural classification policy in schema_map decides both."""
    category = classify_column(ColumnInfo(name="", type=column_type))
    return category in ("numeric", "temporal")


def _has_float_bounds(column_type: str) -> bool:
    outer = column_type.split("<", 1)[0].lower()
    return "float" in outer or "double" in outer