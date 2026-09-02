"""Tool contract tests (Architecture.md #11-#14, invariants).

Verifies every registered tool has a valid spec, unique names, and that
identifier-like columns are rejected by the expensive partition-candidate tool."""

import pytest
from pydantic import ValidationError

from sta.context.schema_map import is_identifier_like
from sta.tools.partitions import (
    PARTITION_CANDIDATE_SPEC,
    PartitionCandidateParameters,
)
from sta.tools.registry import DEFAULT_REGISTRY, TOOLS
from sta.tools.spec import ToolSpec


def test_registry_tool_names_are_unique() -> None:
    names = [spec.name for spec in TOOLS.values()]
    assert len(names) == len(set(names))


def test_every_registry_spec_is_complete() -> None:
    for name, spec in DEFAULT_REGISTRY.items():
        assert isinstance(spec, ToolSpec)
        assert spec.name == name
        assert spec.query_version
        assert spec.parameters is not None
        assert spec.result is not None
        assert callable(spec.build_payload)
        assert spec.payload_schema()


def test_partition_candidate_rejects_identifier_like_columns() -> None:
    rejected = ["id", "order_id", "customer_uuid", "event_guid"]
    for column in rejected:
        assert is_identifier_like(column)
        with pytest.raises(ValidationError):
            PartitionCandidateParameters(column=column)


def test_partition_candidate_accepts_non_identifier_columns() -> None:
    params = PartitionCandidateParameters(column="created_at")
    assert params.column == "created_at"


def test_snapshot_history_not_snapshot_scoped() -> None:
    from sta.tools.table_evolution import SNAPSHOT_HISTORY_SPEC

    assert SNAPSHOT_HISTORY_SPEC.snapshot_scoped is False


def test_file_layout_is_snapshot_scoped() -> None:
    from sta.tools.file_layout import FILE_LAYOUT_SPEC

    assert FILE_LAYOUT_SPEC.snapshot_scoped is True


def test_payload_schema_types_are_defined() -> None:
    for spec in DEFAULT_REGISTRY.values():
        schema = spec.payload_schema()
        for field_name, type_name in schema.items():
            assert isinstance(type_name, str)
            assert type_name
