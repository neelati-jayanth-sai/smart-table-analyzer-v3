"""Backend-independent Iceberg-like table metadata contract.

The startup context builder (Architecture.md #8-#10) is deterministic and pure.
Backends never leak into it: local (PyIceberg) and production (IOMETE/Spark)
implementations only have to resolve a table name into a ``TableMetadata``
through the :class:`TableMetadataProvider` seam (Runtime_Environments_UI.md
#2-#4, #10).

Nothing in this module imports PyIceberg, touches a network, or executes
queries. Metadata arrives either as typed models (upcoming backends) or as
explicit fixture dicts shaped like Iceberg table metadata JSON (kebab-case
keys are accepted alongside snake_case, and unrelated metadata keys such as
``table-uuid``/``snapshots`` are ignored so real metadata payloads can be
handed over as-is).
"""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class SchemaField(BaseModel):
    """One Iceberg schema field. ``field_id`` is the historical field identity
    (Architecture.md #16, schema evolution) and is always preserved."""

    model_config = ConfigDict(populate_by_name=True)

    field_id: int = Field(validation_alias=AliasChoices("id", "field-id", "field_id"))
    name: str
    type: str
    required: bool = True
    doc: str | None = None


class TableSchema(BaseModel):
    """A complete Iceberg schema version (one entry of the schema history)."""

    model_config = ConfigDict(populate_by_name=True)

    schema_id: int = Field(validation_alias=AliasChoices("schema-id", "schema_id"))
    fields: list[SchemaField] = Field(default_factory=list)


class PartitionField(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    name: str
    transform: str
    source_id: int = Field(validation_alias=AliasChoices("source-id", "source_id"))
    field_id: int = Field(validation_alias=AliasChoices("field-id", "field_id"))


class PartitionSpec(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    spec_id: int = Field(validation_alias=AliasChoices("spec-id", "spec_id"))
    fields: list[PartitionField] = Field(default_factory=list)


class SortField(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    transform: str = "identity"
    direction: str = "asc"
    null_order: str = Field(
        default="nulls-last",
        validation_alias=AliasChoices("null-order", "null_order"),
    )
    source_id: int = Field(validation_alias=AliasChoices("source-id", "source_id"))


class SortOrder(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    order_id: int = Field(validation_alias=AliasChoices("order-id", "order_id"))
    fields: list[SortField] = Field(default_factory=list)


class TableMetadata(BaseModel):
    """Explicit, backend-independent snapshot of the Iceberg facts needed for
    the startup context. Optional lists (``partition_specs``, ``sort_orders``)
    model metadata an adapter may not be able to supply; the context builder
    degrades honestly in that case instead of inventing values."""

    model_config = ConfigDict(populate_by_name=True)

    table: str = Field(min_length=1)
    format_version: int = Field(
        default=2,
        validation_alias=AliasChoices("format-version", "format_version"),
    )
    schemas: list[TableSchema]
    current_schema_id: int = Field(
        validation_alias=AliasChoices("current-schema-id", "current_schema_id")
    )
    current_snapshot_id: int | str | None = Field(
        default=None,
        validation_alias=AliasChoices("current-snapshot-id", "current_snapshot_id"),
    )
    partition_specs: list[PartitionSpec] | None = Field(
        default=None,
        validation_alias=AliasChoices("partition-specs", "partition_specs"),
    )
    default_spec_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("default-spec-id", "default_spec_id"),
    )
    sort_orders: list[SortOrder] | None = Field(
        default=None,
        validation_alias=AliasChoices("sort-orders", "sort_orders"),
    )
    default_sort_order_id: int | None = Field(
        default=None,
        validation_alias=AliasChoices("default-sort-order-id", "default_sort_order_id"),
    )
    properties: dict[str, str] = Field(default_factory=dict)
    # Measured per-column availability from a backend that inspected data
    # files; when supplied it wins over the property-derived configuration.
    metrics_availability: dict[str, str] | None = None


def table_metadata_from_dict(raw: Mapping[str, Any]) -> TableMetadata:
    """Adapter: explicit dict metadata (Iceberg metadata-JSON kebab-case or
    snake_case) -> :class:`TableMetadata`. The dict must carry ``table``.
    Unknown keys (``table-uuid``, ``location``, ``snapshots``, ...) are ignored.
    """
    return TableMetadata.model_validate(dict(raw))


@runtime_checkable
class TableMetadataProvider(Protocol):
    """Adapter seam for backends (Runtime_Environments_UI.md #4).

    Backends (LocalIcebergBackend via PyIceberg, IometeBackend) implement this
    to resolve a user table name into backend-independent metadata. The
    context builder never imports a backend and never branches on the
    environment.
    """

    def load_table_metadata(self, table_name: str) -> TableMetadata: ...


class StaticMetadataProvider:
    """In-memory :class:`TableMetadataProvider` over typed fixtures.

    Values may be ``TableMetadata`` instances or fixture dicts (converted with
    :func:`table_metadata_from_dict`); the mapping key is used as the table
    name when the value does not name itself. Serves tests and early wiring
    until real backends exist; it performs no I/O.
    """

    def __init__(self, tables: Mapping[str, TableMetadata | Mapping[str, Any]]):
        self._tables: dict[str, TableMetadata] = {}
        for name, metadata in tables.items():
            if isinstance(metadata, TableMetadata):
                self._tables[name] = metadata
            else:
                self._tables[name] = table_metadata_from_dict(
                    {**metadata, "table": metadata.get("table", name)}
                )

    def load_table_metadata(self, table_name: str) -> TableMetadata:
        try:
            return self._tables[table_name]
        except KeyError:
            raise KeyError(f"No metadata fixture registered for table {table_name!r}") from None