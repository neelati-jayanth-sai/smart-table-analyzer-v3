"""Live PyIceberg adapter — the local backend's real-environment seam.

Converts a loaded PyIceberg :class:`~pyiceberg.table.Table` into the
normalized shapes the rest of STA consumes:

- :func:`table_metadata_from_pyiceberg` → ``sta.context.table_metadata.TableMetadata``
  (the ``TableMetadataProvider`` input for the startup context builder),
- :func:`table_fixture_from_pyiceberg` → :class:`LocalTableFixture`
  (the ``LocalIcebergBackend`` input for every tool computation).

This is the only STA module that imports PyIceberg. It supplies Iceberg facts
only — no interpretation, and no reads beyond manifest/file *metadata*
(Runtime_Environments_UI.md #10). Unit tests exercise the pure mappers with
constructed PyIceberg objects; opening real manifest Avro files needs a live
local catalog (Docker), which the adapter isolates behind ``table.io``.
"""

import datetime as dt
import decimal
from typing import Any

from sta.context.table_metadata import (
    PartitionField,
    PartitionSpec,
    SchemaField,
    SortField,
    SortOrder,
    TableMetadata,
    TableSchema,
)
from sta.execution.backends.local import (
    LocalColumnMetrics,
    LocalFileEntry,
    LocalManifestEntry,
    LocalSnapshot,
    LocalTableFixture,
)
from sta.execution.errors import BackendExecutionError


# ---------------------------------------------------------------------------
# Metadata conversion (startup-context seam)
# ---------------------------------------------------------------------------


def table_metadata_from_pyiceberg(table: Any) -> TableMetadata:
    """Convert a PyIceberg table into backend-independent ``TableMetadata``.

    Accepts any object with the PyIceberg ``Table`` surface (``metadata``,
    ``schema()``) so tests can pass lightweight fakes. Preserves field IDs,
    schema/spec/sort-order history and the snapshot id (Architecture.md #16).
    """
    metadata = table.metadata
    return TableMetadata(
        table=_table_name(table),
        format_version=metadata.format_version,
        schemas=[
            TableSchema(
                schema_id=schema.schema_id,
                fields=[_schema_field(field) for field in schema.fields],
            )
            for schema in metadata.schemas
        ],
        current_schema_id=metadata.current_schema_id,
        current_snapshot_id=metadata.current_snapshot_id,
        partition_specs=_partition_specs(metadata.partition_specs or []),
        default_spec_id=metadata.default_spec_id,
        sort_orders=_sort_orders(metadata.sort_orders),
        default_sort_order_id=metadata.default_sort_order_id,
        properties=dict(metadata.properties),
    )


def _schema_field(field: Any) -> SchemaField:
    return SchemaField(
        field_id=field.field_id,
        name=field.name,
        type=_type_name(field.field_type),
        required=field.required,
        doc=field.doc,
    )


def _partition_specs(specs: list[Any] | None) -> list[PartitionSpec] | None:
    if specs is None:
        return None
    return [
        PartitionSpec(
            spec_id=spec.spec_id,
            fields=[
                PartitionField(
                    name=field.name,
                    transform=_type_name(field.transform),
                    source_id=field.source_id,
                    field_id=field.field_id,
                )
                for field in spec.fields
            ],
        )
        for spec in specs
    ]


def _sort_orders(orders: list[Any] | None) -> list[SortOrder] | None:
    if orders is None:
        return None
    return [
        SortOrder(
            order_id=order.order_id,
            fields=[
                SortField(
                    transform=_type_name(field.transform),
                    direction=field.direction.value,
                    null_order=field.null_order.value,
                    source_id=field.source_id,
                )
                for field in order.fields
            ],
        )
        for order in orders
    ]


def _table_name(table: Any) -> str:
    identifier = table.name() if callable(getattr(table, "name", None)) else table.name
    parts = [identifier] if isinstance(identifier, str) else [part for part in identifier if part]
    if not parts:
        raise BackendExecutionError("*", "PyIceberg table has no resolvable name")
    return ".".join(str(part) for part in parts)


def _type_name(type_: Any) -> str:
    """PyIceberg type/transform → compact Iceberg string (``long``,
    ``timestamp``, ``decimal(10,2)``, ``list<string>``, ``day``)."""
    return str(type_)


# ---------------------------------------------------------------------------
# Snapshot / manifest / file conversion (fixture inputs)
# ---------------------------------------------------------------------------


def local_snapshots(snapshots: list[Any]) -> list[LocalSnapshot]:
    """PyIceberg snapshots → normalized snapshot history rows.

    The summary's additional properties are the spec's ``added-data-files``
    style keys, read through the public PyIceberg surface (see
    :func:`summary_properties`); ``operation`` is carried separately as the
    LocalSnapshot field.
    """
    rows: list[LocalSnapshot] = []
    for snapshot in snapshots:
        summary = snapshot.summary
        rows.append(
            LocalSnapshot(
                snapshot_id=snapshot.snapshot_id,
                parent_snapshot_id=snapshot.parent_snapshot_id,
                timestamp_ms=snapshot.timestamp_ms,
                operation=summary.operation.value if summary is not None else None,
                summary=summary_properties(summary) if summary is not None else {},
            )
        )
    return rows


def summary_properties(summary: Any) -> dict[str, str]:
    """Snapshot-summary additional properties through a safe compatibility
    helper — never an unconditional private-attribute reach.

    Prefers the public PyIceberg accessor (``Summary.additional_properties``,
    the supported API). Only when a PyIceberg build does not provide it does
    the helper read the long-standing private attribute, guarded, so older
    builds keep working. A summary exposing neither yields no properties:
    missing counts stay missing, never guessed (Architecture.md #16).
    """
    accessor = getattr(summary, "additional_properties", None)
    if isinstance(accessor, dict):
        return dict(accessor)
    if callable(accessor):
        try:
            value = accessor()
        except Exception:  # noqa: BLE001 - any accessor failure means no properties
            value = None
        if isinstance(value, dict):
            return dict(value)
    legacy = getattr(summary, "_additional_properties", None)
    if isinstance(legacy, dict):
        return dict(legacy)
    return {}


def local_manifest_entry(manifest: Any) -> LocalManifestEntry:
    return LocalManifestEntry(
        manifest_path=manifest.manifest_path,
        manifest_length_bytes=manifest.manifest_length,
        content=int(manifest.content),
        partition_spec_id=manifest.partition_spec_id,
        added_files_count=manifest.added_files_count,
        existing_files_count=manifest.existing_files_count,
        deleted_files_count=manifest.deleted_files_count,
    )


def local_file_entry(data_file: Any, partition_field_names: list[str]) -> LocalFileEntry:
    """PyIceberg DataFile → normalized file entry.

    ``partition`` values render positionally against the file's own spec
    field names (partition evolution means different files carry different
    spec layouts; unmatched values fall back to positional names instead of
    being dropped).
    """
    values = list(data_file.partition) if data_file.partition is not None else []
    if len(values) == len(partition_field_names):
        partition = {
            name: _render_partition_value(value)
            for name, value in zip(partition_field_names, values)
        }
    else:
        partition = {
            f"partition_{index}": _render_partition_value(value)
            for index, value in enumerate(values)
        }
    return LocalFileEntry(
        file_path=data_file.file_path,
        file_size_bytes=data_file.file_size_in_bytes,
        content=int(data_file.content),
        file_format=str(getattr(data_file.file_format, "value", data_file.file_format)).lower(),
        record_count=data_file.record_count,
        partition=partition,
        partition_spec_id=data_file.spec_id,
        sort_order_id=data_file.sort_order_id,
    )


def _render_partition_value(value: Any) -> str:
    """One partition value as its contract string rendering (dates/timestamps
    ISO-8601, numbers and decimals exactly)."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float, decimal.Decimal)):
        return str(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    return str(value)


def local_column_metrics(
    data_files: list[Any],
    fields: list[Any],
) -> dict[str, list[LocalColumnMetrics]]:
    """Per-column, per-file Iceberg metrics decoded into typed values.

    ``value_counts`` / ``null_value_counts`` / ``nan_value_counts`` and the
    byte bounds maps are keyed by field ID. Metrics the writer did not record
    stay ``None`` (missing metrics do not mean missing data, Architecture.md
    #16). Fields with no reported metrics produce no entry.
    """
    type_by_id = {field.field_id: field.field_type for field in fields}
    name_by_id = {field.field_id: field.name for field in fields}
    metrics: dict[str, list[LocalColumnMetrics]] = {}
    for data_file in data_files:
        value_counts = data_file.value_counts or {}
        null_counts = data_file.null_value_counts or {}
        nan_counts = data_file.nan_value_counts or {}
        lower_bounds = data_file.lower_bounds or {}
        upper_bounds = data_file.upper_bounds or {}
        reported = (
            set(value_counts)
            | set(null_counts)
            | set(nan_counts)
            | set(lower_bounds)
            | set(upper_bounds)
        )
        for field_id in sorted(reported):
            name = name_by_id.get(field_id)
            if name is None:
                continue  # metrics of a dropped field: not part of this schema
            column_type = type_by_id.get(field_id)
            metrics.setdefault(name, []).append(
                LocalColumnMetrics(
                    file_path=data_file.file_path,
                    value_count=value_counts.get(field_id),
                    null_count=null_counts.get(field_id),
                    nan_count=nan_counts.get(field_id),
                    lower_bound=_decode_bound(column_type, lower_bounds.get(field_id)),
                    upper_bound=_decode_bound(column_type, upper_bounds.get(field_id)),
                )
            )
    return metrics


def _decode_bound(column_type: Any, bound: bytes | None) -> int | float | str | None:
    """Decode one Iceberg lower/upper-bound byte string via PyIceberg's
    per-type conversion. Numeric types stay numeric (the aggregate tool
    compares them numerically); every other type renders deterministically as
    a string (dates/timestamps as ISO-8601, decimals exactly). An
    undecodable bound is reported as absent, never guessed."""
    if bound is None:
        return None
    try:
        from pyiceberg.conversions import from_bytes  # noqa: PLC0415 - boundary import

        value = from_bytes(column_type, bound)
    except Exception:  # noqa: BLE001 - undecodable bounds render as None
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    return str(value)


# ---------------------------------------------------------------------------
# Full-table conversion
# ---------------------------------------------------------------------------


def table_fixture_from_pyiceberg(table: Any, snapshot_id: int | None = None) -> LocalTableFixture:
    """Build the normalized :class:`LocalTableFixture` for one PyIceberg table.

    ``snapshot_id`` selects the measured snapshot; ``None`` uses the table's
    current snapshot. Metadata (schema, specs, sort orders, snapshot history,
    properties) comes from ``table.metadata``; manifests, files and per-file
    column metrics are read through ``table.io`` for the resolved snapshot
    only — no data files are opened. A table without snapshots resolves to
    ``snapshot_id=None`` (the absence is preserved, never fabricated as 0).
    """
    metadata = table.metadata
    resolved = snapshot_id if snapshot_id is not None else metadata.current_snapshot_id

    manifests: list[LocalManifestEntry] = []
    data_files: list[LocalFileEntry] = []
    raw_data_files: list[Any] = []
    delete_files: list[LocalFileEntry] = []

    snapshot = _find_snapshot(metadata.snapshots, resolved)
    if snapshot is not None:
        try:
            manifest_files = snapshot.manifests(table.io)
        except Exception as exc:  # noqa: BLE001 - any io failure is typed
            raise BackendExecutionError(
                "*", f"could not read the manifest list of snapshot {resolved}"
            ) from exc
        for manifest in manifest_files:
            manifests.append(local_manifest_entry(manifest))
            names = _spec_field_names(metadata, manifest.partition_spec_id)
            for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
                file_entry = local_file_entry(entry.data_file, names)
                if int(entry.data_file.content) == 0:
                    data_files.append(file_entry)
                    raw_data_files.append(entry.data_file)
                else:
                    delete_files.append(file_entry)

    fields = table.schema().fields
    return LocalTableFixture(
        table=_table_name(table),
        snapshot_id=resolved,
        schema_fields=[_schema_field(field) for field in fields],
        snapshots=local_snapshots(list(metadata.snapshots or [])),
        manifests=manifests,
        data_files=data_files,
        delete_files=delete_files,
        column_metrics=local_column_metrics(raw_data_files, fields),
        column_profiles={},
        partition_specs=_partition_specs(metadata.partition_specs) or [],
        sort_orders=_sort_orders(metadata.sort_orders) or [],
        default_spec_id=metadata.default_spec_id,
        default_sort_order_id=metadata.default_sort_order_id,
    )


def _find_snapshot(snapshots: list[Any], snapshot_id: int | None) -> Any | None:
    if snapshot_id is None:
        return None
    for snapshot in snapshots:
        if snapshot.snapshot_id == snapshot_id:
            return snapshot
    return None


def _spec_field_names(metadata: Any, spec_id: int) -> list[str]:
    for spec in metadata.partition_specs or []:
        if spec.spec_id == spec_id:
            return [field.name for field in spec.fields]
    return []


__all__ = [
    "local_column_metrics",
    "local_file_entry",
    "local_manifest_entry",
    "local_snapshots",
    "summary_properties",
    "table_fixture_from_pyiceberg",
    "table_metadata_from_pyiceberg",
]