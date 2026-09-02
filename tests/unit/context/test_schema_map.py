"""Schema compression tests: structural classification, identifier policy and
column grouping (Architecture.md #9-#10). These validate the existing policy
that the startup context builder must reuse unchanged."""

import pytest

from sta.context.schema_map import (
    ColumnInfo,
    classify_column,
    group_columns,
    is_identifier_like,
)


def col(name: str, type_: str) -> ColumnInfo:
    return ColumnInfo(name=name, type=type_, field_id=None)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("id", True),
        ("uuid", True),
        ("guid", True),
        ("order_id", True),
        ("customer_uuid", True),
        ("device_guid", True),
        ("ID", True),
        ("Order_Id", True),
        ("created_at", False),
        ("identifier", False),  # substring "id" alone is not the policy
        ("identity_card", False),
    ],
)
def test_identifier_policy(name: str, expected: bool):
    assert is_identifier_like(name) is expected


@pytest.mark.parametrize(
    ("name", "type_", "expected"),
    [
        ("order_id", "long", "identifier-like"),  # name wins over type
        ("created_at", "timestamptz", "temporal"),
        ("event_time", "time", "temporal"),
        ("event_day", "date", "temporal"),
        ("amount", "decimal(12,2)", "numeric"),
        ("quantity", "long", "numeric"),
        ("score", "double", "numeric"),
        ("status", "string", "string"),
        ("name", "varchar(64)", "string"),
        ("is_active", "boolean", "boolean"),
        ("payload", "binary", "binary"),
        ("tags", "list<string>", "complex"),
        ("address", "struct<street: string>", "complex"),
        ("attrs", "map<string, string>", "complex"),
        ("extra", "variant", "other"),
        ("is_id", "boolean", "identifier-like"),  # precedence: name before type
        ("count_per_day", "long", "numeric"),  # ends _day, not _id
    ],
)
def test_classify_column(name: str, type_: str, expected: str):
    assert classify_column(col(name, type_)) == expected


def test_classify_name_suffix_beats_type():
    # "count_per_day" above ends with "_day", not "_id"; assert the real
    # precedence rule: a *_id name is identifier-like even when temporal.
    assert classify_column(col("snapshot_id", "timestamptz")) == "identifier-like"


def test_group_columns_order_and_drops_empty_groups():
    columns = [
        col("order_id", "long"),
        col("created_at", "timestamptz"),
        col("amount", "decimal(12,2)"),
        col("status", "string"),
        col("region", "string"),
        col("is_active", "boolean"),
    ]
    groups = group_columns(columns)
    assert groups == {
        "identifier-like": ["order_id"],
        "temporal": ["created_at"],
        "numeric": ["amount"],
        "string": ["status", "region"],
        "boolean": ["is_active"],
    }
    # field order is preserved inside groups
    assert groups["string"] == ["status", "region"]


def test_group_columns_covers_every_structural_category():
    columns = [
        col("id", "long"),
        col("ts", "timestamptz"),
        col("n", "int"),
        col("s", "string"),
        col("b", "boolean"),
        col("bin", "binary"),
        col("c", "list<int>"),
        col("o", "variant"),
    ]
    groups = group_columns(columns)
    assert set(groups) == {
        "identifier-like",
        "temporal",
        "numeric",
        "string",
        "boolean",
        "binary",
        "complex",
        "other",
    }


def test_group_columns_empty_schema():
    assert group_columns([]) == {}