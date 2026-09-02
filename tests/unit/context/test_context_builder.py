"""Startup context builder tests (Architecture.md #8-#10, #15-#16).

The builder must convert explicit Iceberg-like metadata into the compact
TableContext without raw DDL, preserving schema_id, field IDs, partition/sort
IDs, snapshot, format version, grouped structural schema, relevant safe
properties, metrics availability and the R000 full-schema reference."""

import pytest

from sta.context.context_builder import (
    UNPARTITIONED,
    describe_metrics_availability,
    render_transform,
)
from sta.context.table_context import FULL_SCHEMA_REF
from sta.context.table_metadata import TableMetadata, table_metadata_from_dict

from sta.context import build_startup_context

ORDERS_METADATA = {
    "format-version": 2,
    "current-schema-id": 1,
    "schemas": [
        {
            "schema-id": 0,
            "fields": [
                {"id": 1, "name": "order_id", "required": True, "type": "long"},
                {"id": 2, "name": "status", "required": True, "type": "string"},
                {"id": 3, "name": "created_at", "required": True, "type": "timestamptz"},
            ],
        },
        {
            "schema-id": 1,
            "fields": [
                {"id": 1, "name": "order_id", "required": True, "type": "long"},
                {"id": 2, "name": "status", "required": True, "type": "string"},
                {"id": 3, "name": "created_at", "required": True, "type": "timestamptz"},
                {"id": 4, "name": "customer_id", "required": False, "type": "long",
                 "doc": "placing customer"},
                {"id": 5, "name": "amount", "required": False, "type": "decimal(12,2)"},
                {"id": 6, "name": "is_active", "required": False, "type": "boolean"},
                {"id": 7, "name": "payload", "required": False, "type": "binary"},
                {"id": 8, "name": "tags", "required": False, "type": "list<string>"},
                {"id": 9, "name": "note", "required": False, "type": "string"},
            ],
        },
    ],
    "partition-specs": [
        {
            "spec-id": 0,
            "fields": [
                {"name": "created_at_day", "transform": "identity", "source-id": 3,
                 "field-id": 1000},
            ],
        },
        {
            "spec-id": 1,
            "fields": [
                {"name": "created_at_day", "transform": "day", "source-id": 3,
                 "field-id": 1001},
                {"name": "order_bucket", "transform": "bucket[16]", "source-id": 1,
                 "field-id": 1002},
            ],
        },
    ],
    "default-spec-id": 1,
    "sort-orders": [
        {"order-id": 1, "fields": []},
        {
            "order-id": 2,
            "fields": [
                {"transform": "identity", "direction": "asc", "null-order": "nulls-first",
                 "source-id": 3},
                {"transform": "truncate[10]", "direction": "desc", "null-order": "nulls-last",
                 "source-id": 9},
            ],
        },
    ],
    "default-sort-order-id": 2,
    "current-snapshot-id": 9182781280348117982,
    "properties": {
        "write.format.default": "parquet",
        "write.distribution-mode": "hash",
        "write.metadata.metrics.default": "truncate(16)",
        "write.delete.mode": "merge-on-read",
        "write.target-file-size-bytes": "134217728",
        "team.custom.property": "irrelevant-for-analysis",
        "s3.secret-access-key": "must-never-leak",
    },
}


def orders_metadata() -> dict:
    return {"table": "prod.sales.orders", **ORDERS_METADATA}


def orders_context():
    return build_startup_context(orders_metadata())


def empty_schema() -> dict:
    """Minimal consistent metadata: one schema version, no fields."""
    return {"schema-id": 0, "fields": []}


def minimal_metadata(**overrides) -> dict:
    md = {"table": "t", "schemas": [empty_schema()], "current-schema-id": 0}
    md.update(overrides)
    return md


# -- preservation ------------------------------------------------------------


def test_full_v2_metadata_preserves_everything():
    ctx = orders_context().table_context
    assert ctx.table == "prod.sales.orders"
    assert ctx.snapshot_id == "9182781280348117982"  # int64 snapshot kept verbatim
    assert ctx.format_version == 2
    assert ctx.schema_id == 1  # current schema, not the first in the history
    assert ctx.partition_spec_id == 1
    assert ctx.sort_order_id == 2
    assert ctx.current_partition_spec == "days(created_at), bucket[16](order_id)"
    assert ctx.current_sort_order == "created_at ASC NULLS FIRST, truncate[10](note) DESC NULLS LAST"
    assert ctx.full_schema_ref == "R000"
    assert ctx.workload_analysis == "disabled"


def test_schema_summary_preserves_field_ids_in_order():
    ctx = orders_context().table_context
    assert [(c.name, c.type, c.field_id) for c in ctx.schema_summary] == [
        ("order_id", "long", 1),
        ("status", "string", 2),
        ("created_at", "timestamptz", 3),
        ("customer_id", "long", 4),
        ("amount", "decimal(12,2)", 5),
        ("is_active", "boolean", 6),
        ("payload", "binary", 7),
        ("tags", "list<string>", 8),
        ("note", "string", 9),
    ]


def test_column_groups_come_from_existing_schema_map():
    ctx = orders_context().table_context
    assert ctx.column_groups == {
        "identifier-like": ["order_id", "customer_id"],
        "temporal": ["created_at"],
        "numeric": ["amount"],
        "string": ["status", "note"],
        "boolean": ["is_active"],
        "binary": ["payload"],
        "complex": ["tags"],
    }


def test_schema_evolution_uses_current_schema():
    ctx = orders_context().table_context
    # schema 0 has three fields; schema 1 (current) has nine
    assert len(ctx.schema_summary) == 9
    full = orders_context().full_schema
    assert full.schema_id == 1
    assert [s.schema_id for s in full.schemas] == [0, 1]
    assert full.fields[3].doc == "placing customer"
    assert full.fields[3].required is False


def test_accepts_typed_metadata_instance():
    md = table_metadata_from_dict(orders_metadata())
    typed = build_startup_context(md).table_context
    from_dict = build_startup_context(orders_metadata()).table_context
    assert typed == from_dict


# -- partition spec ----------------------------------------------------------


@pytest.mark.parametrize(
    ("transform", "expected"),
    [
        ("identity", "created_at"),
        ("year", "years(created_at)"),
        ("month", "months(created_at)"),
        ("day", "days(created_at)"),
        ("hour", "hours(created_at)"),
        ("bucket[16]", "bucket[16](created_at)"),
        ("truncate[10]", "truncate[10](created_at)"),
        ("void", "void"),
        ("unknown_transform", "unknown_transform(created_at)"),
    ],
)
def test_render_transform(transform: str, expected: str):
    assert render_transform(transform, "created_at") == expected


def test_partition_spec_defaults_when_metadata_absent():
    md = table_metadata_from_dict(minimal_metadata(**{"default-spec-id": 7}))
    ctx = build_startup_context(md).table_context
    # no spec list supplied: id is preserved, the rendering is honest
    assert ctx.partition_spec_id == 7
    assert ctx.current_partition_spec == "unknown"
    assert ctx.partition_spec_history_available is False


def test_partition_spec_fully_absent_is_unpartitioned():
    ctx = build_startup_context(minimal_metadata()).table_context
    assert ctx.current_partition_spec == "unpartitioned"
    assert ctx.partition_spec_id is None
    assert ctx.partition_spec_history_available is False


def test_partition_spec_history_flag_requires_multiple_specs():
    single_spec = minimal_metadata(
        **{
            "partition-specs": [
                {"spec-id": 0, "fields": [{"name": "d", "transform": "day", "source-id": 1,
                                           "field-id": 1000}]}
            ],
            "default-spec-id": 0,
        }
    )
    ctx = build_startup_context(single_spec).table_context
    assert ctx.current_partition_spec == "days(d)"  # unknown source falls back to field name
    assert ctx.partition_spec_history_available is False

    evolved = minimal_metadata(
        **{
            "partition-specs": single_spec["partition-specs"]
            + [{"spec-id": 1, "fields": []}],
            "default-spec-id": 1,
        }
    )
    ctx2 = build_startup_context(evolved).table_context
    assert ctx2.partition_spec_history_available is True
    assert ctx2.current_partition_spec == "unpartitioned"  # spec 1 has no fields


def test_partition_spec_void_transform_uses_partition_field_name():
    md = table_metadata_from_dict(
        minimal_metadata(
            **{
                "partition-specs": [
                    {
                        "spec-id": 0,
                        "fields": [
                            # dropped source column: void transform, no source field
                            {"name": "old_day", "transform": "void", "source-id": 42,
                             "field-id": 1000},
                        ],
                    }
                ],
                "default-spec-id": 0,
            }
        )
    )
    ctx = build_startup_context(md).table_context
    assert ctx.current_partition_spec == "void"


def test_unknown_default_spec_id_raises():
    md = table_metadata_from_dict(
        minimal_metadata(
            **{
                "partition-specs": [{"spec-id": 0, "fields": []}],
                "default-spec-id": 5,
            }
        )
    )
    with pytest.raises(ValueError, match="partition spec id 5"):
        build_startup_context(md)


# -- sort order --------------------------------------------------------------


def test_sort_order_defaults_and_unsorted():
    ctx = build_startup_context(minimal_metadata()).table_context
    assert ctx.current_sort_order == "none"
    assert ctx.sort_order_id is None

    unsorted = table_metadata_from_dict(
        minimal_metadata(
            **{
                "sort-orders": [{"order-id": 1, "fields": []}],
                "default-sort-order-id": 1,
            }
        )
    )
    ctx2 = build_startup_context(unsorted).table_context
    assert ctx2.current_sort_order == "none"
    assert ctx2.sort_order_id == 1  # preserved even for the unsorted order


def test_unknown_default_sort_order_id_raises():
    md = table_metadata_from_dict(
        minimal_metadata(
            **{
                "sort-orders": [{"order-id": 1, "fields": []}],
                "default-sort-order-id": 9,
            }
        )
    )
    with pytest.raises(ValueError, match="sort order id 9"):
        build_startup_context(md)


# -- properties --------------------------------------------------------------


def test_relevant_safe_properties_only():
    ctx = orders_context().table_context
    assert ctx.relevant_table_properties == {
        "write.delete.mode": "merge-on-read",
        "write.distribution-mode": "hash",
        "write.format.default": "parquet",
        "write.metadata.metrics.default": "truncate(16)",
        "write.target-file-size-bytes": "134217728",
    }


def test_properties_are_curated_for_the_full_schema_record_too():
    full = orders_context().full_schema
    assert "s3.secret-access-key" not in full.properties
    assert "team.custom.property" not in full.properties
    assert full.properties == orders_context().table_context.relevant_table_properties


def test_properties_are_curated_for_the_full_schema_record_too():
    full = orders_context().full_schema
    assert "s3.secret-access-key" not in full.properties
    assert "team.custom.property" not in full.properties
    assert full.properties == orders_context().table_context.relevant_table_properties


def test_relevant_properties_keep_known_case_variants_verbatim():
    """Known Iceberg keys written with non-canonical casing stay in the
    context with their raw key so the Investigator can notice inert case
    variants; they never overwrite the canonical lowercase key or each
    other, and unknown/secret keys stay dropped even when uppercased."""
    ctx = build_startup_context(
        {
            **orders_metadata(),
            "properties": {
                **ORDERS_METADATA["properties"],
                "WRITE.TARGET-FILE-SIZE-BYTES": "134217728",  # inert case variant
                "Write.Target-File-Size-Bytes": "64000000",  # another variant
                "WRITE.METADATA.METRICS.DEFAULT": "none",
                "S3.SECRET-ACCESS-KEY": "must-never-leak",  # unknown: stays dropped
                "TEAM.CUSTOM.PROPERTY": "irrelevant-for-analysis",
            },
        }
    ).table_context
    assert ctx.relevant_table_properties == {
        # canonical lowercase keys remain supported verbatim
        "write.delete.mode": "merge-on-read",
        "write.distribution-mode": "hash",
        "write.format.default": "parquet",
        "write.metadata.metrics.default": "truncate(16)",
        "write.target-file-size-bytes": "134217728",
        # case variants of known keys, preserved exactly as stored
        "WRITE.METADATA.METRICS.DEFAULT": "none",
        "Write.Target-File-Size-Bytes": "64000000",
        "WRITE.TARGET-FILE-SIZE-BYTES": "134217728",
    }
    # R000 carries the same curated set, case variants included.
    startup = build_startup_context(
        {
            **orders_metadata(),
            "properties": {
                **ORDERS_METADATA["properties"],
                "WRITE.TARGET-FILE-SIZE-BYTES": "134217728",
            },
        }
    )
    assert (
        startup.full_schema.properties
        == startup.table_context.relevant_table_properties
        == {
            "write.delete.mode": "merge-on-read",
            "write.distribution-mode": "hash",
            "write.format.default": "parquet",
            "write.metadata.metrics.default": "truncate(16)",
            "write.target-file-size-bytes": "134217728",
            "WRITE.TARGET-FILE-SIZE-BYTES": "134217728",
        }
    )


def test_case_variants_do_not_drive_metrics_availability():
    """Only exact lowercase keys feed derived facts: an uppercase metrics
    variant stays visible in the context but never overrides the effective
    (or default) metrics configuration."""
    ctx = build_startup_context(
        {
            **orders_metadata(),
            "properties": {
                **ORDERS_METADATA["properties"],
                "WRITE.METADATA.METRICS.DEFAULT": "none",  # inert variant
            },
        }
    ).table_context
    assert ctx.relevant_table_properties["WRITE.METADATA.METRICS.DEFAULT"] == "none"
    assert set(ctx.metrics_availability.values()) == {"truncated bounds + counts available"}


# -- metrics availability ----------------------------------------------------


def test_metrics_default_truncate_mode():
    ctx = orders_context().table_context
    assert ctx.metrics_availability == {
        "order_id": "truncated bounds + counts available",
        "status": "truncated bounds + counts available",
        "created_at": "truncated bounds + counts available",
        "customer_id": "truncated bounds + counts available",
        "amount": "truncated bounds + counts available",
        "is_active": "truncated bounds + counts available",
        "payload": "truncated bounds + counts available",
        "tags": "truncated bounds + counts available",
        "note": "truncated bounds + counts available",
    }


def test_metrics_modes_none_counts_and_type_scoped_bounds():
    fields = [
        {"id": 1, "name": "n", "type": "long"},
        {"id": 2, "name": "f", "type": "double"},
        {"id": 3, "name": "s", "type": "string"},
        {"id": 4, "name": "t", "type": "timestamp"},
    ]
    base = {
        "table": "t",
        "schemas": [{"schema-id": 0, "fields": fields}],
        "current-schema-id": 0,
    }
    none_ctx = build_startup_context(
        {**base, "properties": {"write.metadata.metrics.default": "none"}}
    ).table_context
    assert set(none_ctx.metrics_availability.values()) == {"none"}

    counts_ctx = build_startup_context(
        {**base, "properties": {"write.metadata.metrics.default": "counts"}}
    ).table_context
    assert set(counts_ctx.metrics_availability.values()) == {"counts only"}

    integers_ctx = build_startup_context(
        {**base, "properties": {"write.metadata.metrics.default": "integers"}}
    ).table_context
    assert integers_ctx.metrics_availability == {
        "n": "bounds + counts available",
        "f": "bounds + counts available",  # numeric per the structural classification
        "s": "counts only",
        "t": "bounds + counts available",
    }

    floats_ctx = build_startup_context(
        {**base, "properties": {"write.metadata.metrics.default": "floats"}}
    ).table_context
    assert floats_ctx.metrics_availability == {
        "n": "counts only",
        "f": "bounds + counts available",
        "s": "counts only",
        "t": "counts only",
    }


def test_metrics_column_overrides_by_field_id_and_name():
    ctx = build_startup_context(
        {
            **orders_metadata(),
            "properties": {
                **ORDERS_METADATA["properties"],
                "write.metadata.metrics.column.2": "none",  # by field id (status)
                "write.metadata.metrics.column.amount": "counts",  # by name
            },
        }
    ).table_context
    assert ctx.metrics_availability["status"] == "none"
    assert ctx.metrics_availability["amount"] == "counts only"
    assert ctx.metrics_availability["order_id"] == "truncated bounds + counts available"


def test_explicit_metrics_availability_wins_and_is_filtered_to_schema():
    md = table_metadata_from_dict(
        {
            **orders_metadata(),
            "metrics_availability": {"created_at": "bounds + counts available", "ghost": "x"},
        }
    )
    ctx = build_startup_context(md).table_context
    assert ctx.metrics_availability == {"created_at": "bounds + counts available"}


def test_describe_metrics_availability_passes_unknown_modes_through():
    assert describe_metrics_availability("custom-mode", "string") == "custom-mode"


# -- errors and input forms --------------------------------------------------


def test_unknown_current_schema_id_raises():
    md = table_metadata_from_dict(
        {"table": "t", "schemas": [{"schema-id": 0, "fields": []}], "current-schema-id": 4}
    )
    with pytest.raises(ValueError, match="current_schema_id 4"):
        build_startup_context(md)


def test_builder_accepts_table_metadata_instance_and_mapping():
    md = TableMetadata.model_validate({**orders_metadata()})
    from_model = build_startup_context(md)
    from_mapping = build_startup_context(orders_metadata())
    assert from_model == from_mapping


# -- full schema record (R000) -----------------------------------------------


def test_full_schema_record_carries_complete_structural_metadata():
    startup = orders_context()
    full = startup.full_schema
    assert full.ref == FULL_SCHEMA_REF == "R000"
    assert full.table == "prod.sales.orders"
    assert full.snapshot_id == startup.table_context.snapshot_id
    assert full.schema_id == startup.table_context.schema_id == 1
    assert full.format_version == 2
    assert [(f.field_id, f.name, f.required) for f in full.fields][:2] == [
        (1, "order_id", True),
        (2, "status", True),
    ]
    assert len(full.schemas) == 2  # full schema history for evolution questions
    assert [s.spec_id for s in full.partition_specs] == [0, 1]
    assert [o.order_id for o in full.sort_orders] == [1, 2]


def test_startup_context_is_consistent_between_parts():
    startup = orders_context()
    assert startup.table_context.full_schema_ref == startup.full_schema.ref
    assert startup.table_context.snapshot_id == startup.full_schema.snapshot_id
    assert len(startup.table_context.schema_summary) == len(startup.full_schema.fields)


# -- minimal / edge metadata -------------------------------------------------


def test_minimal_metadata_builds_empty_compact_context():
    ctx = build_startup_context(
        minimal_metadata(table="fresh.table")
    ).table_context
    assert ctx.table == "fresh.table"
    assert ctx.snapshot_id is None
    assert ctx.schema_summary == []
    assert ctx.column_groups == {}
    assert ctx.current_partition_spec == "unpartitioned"
    assert ctx.current_sort_order == "none"
    assert ctx.relevant_table_properties == {}
    assert ctx.metrics_availability == {}
    assert ctx.full_schema_ref == "R000"


def test_unpartitioned_constant_matches_model_default():
    assert UNPARTITIONED == "unpartitioned"