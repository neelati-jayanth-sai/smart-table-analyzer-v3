"""IOMETE backend tests (Runtime_Environments_UI.md #5-#7, #12).

Tests the production seam with a stub connection so no external IOMETE or
Spark is required."""

from collections.abc import Mapping
from typing import Any

import pytest

from sta.context.table_metadata import TableMetadata
from sta.execution.backends.iomete import IometeBackend, IometeConnection
from sta.execution.errors import (
    BackendExecutionError,
    SnapshotNotAvailableError,
)
from sta.execution.queries.loader import iomete_template_tools


class _StubIometeConnection:
    """Deterministic stub: returns the configured rows for every execute() call."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql_log: list[str] = []

    def execute(self, sql: str) -> list[dict[str, Any]]:
        self.sql_log.append(sql)
        return list(self.rows)


def _rows_for(tool_name: str) -> list[dict[str, Any]]:
    """Return fixture rows that satisfy the tool contract."""
    rows: dict[str, list[dict[str, Any]]] = {
        "get_file_layout": [
            {
                "file_count": 3,
                "total_size_bytes": 31457280,
                "total_record_count": 300,
                "min_file_size_bytes": 10485760,
                "max_file_size_bytes": 10485760,
                "avg_file_size_bytes": 10485760.0,
                "median_file_size_bytes": 10485760.0,
                "p25_file_size_bytes": 10485760.0,
                "p90_file_size_bytes": 10485760.0,
                "p95_file_size_bytes": 10485760.0,
                "min_record_count": 100,
                "max_record_count": 100,
                "median_record_count": 100.0,
            }
        ],
        "get_snapshot_history": [
            {
                "snapshot_id": "9182781280348117982",
                "parent_snapshot_id": "1111111111111111111",
                "timestamp_ms": 1700000000000,
                "operation": "append",
                "added_data_files": 3,
                "removed_data_files": 0,
                "added_records": 300,
                "removed_records": 0,
            }
        ],
        "get_partition_spec_usage": [
            {"spec_id": 1, "file_count": 3, "total_size_bytes": 31457280},
        ],
        "get_sort_order_usage": [
            {"sort_order_id": 2, "file_count": 2, "total_size_bytes": 20971520},
            {"sort_order_id": None, "file_count": 1, "total_size_bytes": 10485760},
        ],
        "get_iomete_maintenance_config": [
            {"key": "auto-optimize.enabled", "value": "true", "source": "table"},
        ],
    }
    return rows.get(tool_name, [])


def test_iomete_supported_tools_require_maintenance_table(table_metadata: TableMetadata) -> None:
    backend_without = IometeBackend(
        table="demo.sales.orders",
        connection=_StubIometeConnection([]),
        metadata=table_metadata,
    )
    assert "get_iomete_maintenance_config" not in backend_without.supported_tools()

    backend_with = IometeBackend(
        table="demo.sales.orders",
        connection=_StubIometeConnection([]),
        maintenance_table="demo.maintenance.config",
        metadata=table_metadata,
    )
    assert "get_iomete_maintenance_config" in backend_with.supported_tools()


def test_iomete_execute_returns_normalized_rows(table_metadata: TableMetadata) -> None:
    connection = _StubIometeConnection(_rows_for("get_file_layout"))
    backend = IometeBackend(
        table="demo.sales.orders",
        connection=connection,
        metadata=table_metadata,
    )
    result = backend.execute("get_file_layout", {}, snapshot_id="9182781280348117982")
    assert result.rows[0]["file_count"] == 3
    assert result.snapshot_id == "9182781280348117982"


def test_iomete_snapshot_required_raises(table_metadata: TableMetadata) -> None:
    backend = IometeBackend(
        table="demo.sales.orders",
        connection=_StubIometeConnection([]),
        metadata=table_metadata,
    )
    with pytest.raises(SnapshotNotAvailableError):
        backend.execute("get_file_layout", {}, snapshot_id=None)


def test_iomete_partition_spec_usage_enriched_from_metadata(table_metadata: TableMetadata) -> None:
    connection = _StubIometeConnection(_rows_for("get_partition_spec_usage"))
    backend = IometeBackend(
        table="demo.sales.orders",
        connection=connection,
        metadata=table_metadata,
    )
    result = backend.execute("get_partition_spec_usage", {}, snapshot_id="9182781280348117982")
    assert result.rows[0]["fields"] == ["days(created_at)", "bucket[16](order_id)"]


def test_iomete_maintenance_config_execution(table_metadata: TableMetadata) -> None:
    connection = _StubIometeConnection(_rows_for("get_iomete_maintenance_config"))
    backend = IometeBackend(
        table="demo.sales.orders",
        connection=connection,
        maintenance_table="demo.maintenance.config",
        metadata=table_metadata,
    )
    result = backend.execute("get_iomete_maintenance_config", {}, snapshot_id=None)
    assert result.snapshot_id is None
    assert result.rows[0]["key"] == "auto-optimize.enabled"


def test_iomete_unknown_tool_raises(table_metadata: TableMetadata) -> None:
    backend = IometeBackend(
        table="demo.sales.orders",
        connection=_StubIometeConnection([]),
        metadata=table_metadata,
    )
    with pytest.raises(BackendExecutionError) as exc_info:
        backend.execute("no_such_tool", {}, snapshot_id="123")
    assert "unknown tool" in str(exc_info.value).lower()
