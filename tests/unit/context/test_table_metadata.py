"""Table metadata contract tests: typed models, the fixture-dict adapter
(Iceberg metadata-JSON kebab-case and snake_case) and the backend provider
seam (Runtime_Environments_UI.md #2-#4, #10)."""

import pytest
from pydantic import ValidationError

from sta.context.table_metadata import (
    PartitionSpec,
    StaticMetadataProvider,
    TableMetadata,
    TableMetadataProvider,
    table_metadata_from_dict,
)

ICEBERG_JSON = {
    "format-version": 2,
    "table-uuid": "b7f1a0c1-6d5e-4a1e-9d2a-2f0c8e6f1a10",
    "location": "s3://warehouse/sales/orders",
    "last-updated-ms": 1735689600000,
    "snapshots": [{"snapshot-id": 1}],
    "current-schema-id": 1,
    "schemas": [
        {"schema-id": 0, "fields": [{"id": 1, "name": "order_id", "required": True, "type": "long"}]},
        {
            "schema-id": 1,
            "fields": [
                {"id": 1, "name": "order_id", "required": True, "type": "long"},
                {"id": 2, "name": "created_at", "required": True, "type": "timestamptz",
                 "doc": "order placement time"},
            ],
        },
    ],
    "partition-specs": [
        {
            "spec-id": 1,
            "fields": [
                {"name": "created_at_day", "transform": "day", "source-id": 2, "field-id": 1001},
            ],
        }
    ],
    "default-spec-id": 1,
    "sort-orders": [
        {"order-id": 1, "fields": []},
        {
            "order-id": 2,
            "fields": [
                {"transform": "identity", "direction": "asc", "null-order": "nulls-first",
                 "source-id": 2},
            ],
        },
    ],
    "default-sort-order-id": 2,
    "current-snapshot-id": 9182781280348117982,
    "properties": {"write.metadata.metrics.default": "truncate(16)"},
}


def test_parses_iceberg_metadata_json_fixture():
    md = table_metadata_from_dict({"table": "prod.sales.orders", **ICEBERG_JSON})
    assert md.table == "prod.sales.orders"
    assert md.format_version == 2
    assert md.current_schema_id == 1
    assert [f.field_id for f in md.schemas[1].fields] == [1, 2]
    assert md.schemas[1].fields[1].doc == "order placement time"
    assert md.current_snapshot_id == 9182781280348117982
    assert md.partition_specs is not None
    spec = md.partition_specs[0]
    assert spec.spec_id == 1
    assert spec.fields[0].source_id == 2
    assert spec.fields[0].field_id == 1001
    assert md.sort_orders is not None
    assert md.sort_orders[0].fields == []
    assert md.sort_orders[1].fields[0].null_order == "nulls-first"
    assert md.properties == {"write.metadata.metrics.default": "truncate(16)"}


def test_parses_snake_case_and_typed_construction():
    md = TableMetadata(
        table="prod.sales.orders",
        format_version=2,
        schemas=[
            {
                "schema_id": 0,
                "fields": [{"field_id": 1, "name": "order_id", "type": "long"}],
            }
        ],
        current_schema_id=0,
    )
    assert md.schemas[0].schema_id == 0
    assert md.schemas[0].fields[0].field_id == 1
    assert md.current_snapshot_id is None
    assert md.partition_specs is None  # adapter may not supply spec history
    assert md.properties == {}


def test_ignores_unrelated_metadata_keys():
    md = table_metadata_from_dict({"table": "t", **ICEBERG_JSON})
    # table-uuid / location / snapshots / last-updated-ms are metadata-file
    # noise for this contract and must not fail validation.
    assert not hasattr(md, "table_uuid")
    assert md.model_dump(exclude_none=True).get("snapshots") is None


def test_requires_table_and_current_schema():
    with pytest.raises(ValidationError):
        table_metadata_from_dict({"current-schema-id": 0, "schemas": []})
    with pytest.raises(ValidationError):
        table_metadata_from_dict({"table": "t", "schemas": []})


def test_snapshot_id_accepts_string():
    md = table_metadata_from_dict(
        {"table": "t", "schemas": [], "current-schema-id": 0, "current-snapshot-id": "91827"}
    )
    assert md.current_snapshot_id == "91827"


def test_static_provider_resolves_fixtures():
    typed = table_metadata_from_dict({"table": "a.typed", **ICEBERG_JSON})
    provider = StaticMetadataProvider(
        {"a.typed": typed, "a.dict": {"current-schema-id": 0, "schemas": []}}
    )
    assert provider.load_table_metadata("a.typed") is typed
    resolved = provider.load_table_metadata("a.dict")
    assert isinstance(resolved, TableMetadata)
    assert resolved.table == "a.dict"


def test_static_provider_unknown_table_raises_keyerror():
    provider = StaticMetadataProvider({})
    with pytest.raises(KeyError, match="nope.table"):
        provider.load_table_metadata("nope.table")


def test_provider_seam_satisfied_by_static_provider():
    # The seam is structural: any backend object able to resolve a table name
    # into TableMetadata can serve the context builder.
    assert isinstance(StaticMetadataProvider({}), TableMetadataProvider)


def test_partition_and_sort_model_defaults():
    spec = PartitionSpec.model_validate({"spec-id": 3})
    assert spec.fields == []
    assert spec.spec_id == 3