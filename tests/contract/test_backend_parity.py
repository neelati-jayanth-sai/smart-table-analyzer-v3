"""Local / IOMETE backend parity tests (Runtime_Environments_UI.md #12, #14).

Both backends must produce the same tool payload contracts for the same
underlying table state. Tests use deterministic fixture data and an IOMETE
stub connection so no Docker or external network is required."""

from typing import Any

import pytest

from sta.context.table_metadata import TableMetadata
from sta.execution.backends.iomete import IometeBackend
from sta.execution.backends.local import (
    LocalColumnProfile,
    LocalFileEntry,
    LocalIcebergBackend,
    LocalManifestEntry,
    LocalSnapshot,
    LocalTableFixture,
)
from sta.execution.runner import QueryRunner
from sta.results.models import RunRecord
from sta.results.store import ResultStore


class _StubIometeConnection:
    """Stub that returns the rows supplied at construction."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def execute(self, sql: str) -> list[dict[str, Any]]:
        return list(self.rows)


def _run_local(tmp_path, fixture: LocalTableFixture, tool_name: str, parameters: dict[str, Any] | None) -> Any:
    store = ResultStore(tmp_path / "local.sqlite3")
    run_id = store.create_run(RunRecord(run_id="run_local", table=fixture.table, started_at="2024-01-01T00:00:00", status="running")).run_id
    backend = LocalIcebergBackend(fixture)
    with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
        outcome = runner.run(tool_name, parameters)
    return outcome.payload


def _run_iomete(
    tmp_path,
    fixture: LocalTableFixture,
    metadata: TableMetadata,
    rows: list[dict[str, Any]],
    tool_name: str,
    parameters: dict[str, Any] | None,
) -> Any:
    store = ResultStore(tmp_path / "iomete.sqlite3")
    run_id = store.create_run(RunRecord(run_id="run_iomete", table=fixture.table, started_at="2024-01-01T00:00:00", status="running")).run_id
    backend = IometeBackend(
        table=fixture.table,
        connection=_StubIometeConnection(rows),
        metadata=metadata,
    )
    with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
        outcome = runner.run(tool_name, parameters)
    return outcome.payload


def test_parity_get_file_layout(tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata) -> None:
    rows = [
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
    ]
    local_payload = _run_local(tmp_path, local_table_fixture, "get_file_layout", None)
    iomete_payload = _run_iomete(tmp_path, local_table_fixture, table_metadata, rows, "get_file_layout", None)
    assert local_payload.model_dump() == iomete_payload.model_dump()


def test_parity_get_snapshot_history(tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata) -> None:
    rows = [
        {
            "snapshot_id": "9182781280348117982",
            "parent_snapshot_id": "1111111111111111111",
            "timestamp_ms": 1700000000000,
            "operation": "append",
            "added_data_files": 3,
            "removed_data_files": 0,
            "added_records": 300,
            "removed_records": 0,
        },
        {
            "snapshot_id": "2222222222222222222",
            "parent_snapshot_id": None,
            "timestamp_ms": 1699000000000,
            "operation": "append",
            "added_data_files": 1,
            "removed_data_files": 0,
            "added_records": 100,
            "removed_records": 0,
        },
    ]
    local_payload = _run_local(tmp_path, local_table_fixture, "get_snapshot_history", None)
    iomete_payload = _run_iomete(tmp_path, local_table_fixture, table_metadata, rows, "get_snapshot_history", None)
    assert local_payload.model_dump() == iomete_payload.model_dump()


def test_parity_get_file_layout_history(tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata) -> None:
    """Cumulative layout totals are strings in real Iceberg snapshot summaries;
    the IOMETE template casts them to BIGINT so both backends converge on the
    same int | None contract."""
    rows = [
        {
            "snapshot_id": "9182781280348117982",
            "timestamp_ms": 1700000000000,
            "operation": "append",
            "total_data_files": 3,
            "total_data_size_bytes": 31457280,
            "total_records": 300,
            "added_data_files": 3,
            "removed_data_files": 0,
        },
        {
            "snapshot_id": "2222222222222222222",
            "timestamp_ms": 1699000000000,
            "operation": "append",
            "total_data_files": 1,
            "total_data_size_bytes": 10485760,
            "total_records": 100,
            "added_data_files": 1,
            "removed_data_files": 0,
        },
    ]
    local_payload = _run_local(tmp_path, local_table_fixture, "get_file_layout_history", None)
    iomete_payload = _run_iomete(
        tmp_path, local_table_fixture, table_metadata, rows, "get_file_layout_history", None
    )
    assert local_payload.model_dump() == iomete_payload.model_dump()
    assert local_payload.entries[0].total_data_files == 3


def _manifest_all_null_counts_fixture(base: LocalTableFixture) -> LocalTableFixture:
    """Manifests where no entry counts are reported; contract must return NULL,
    not invented zeros."""
    return base.model_copy(
        update={
            "manifests": [
                LocalManifestEntry(
                    manifest_path="manifest-a.avro",
                    manifest_length_bytes=100,
                    content=0,
                    partition_spec_id=1,
                    added_files_count=None,
                    existing_files_count=None,
                    deleted_files_count=None,
                ),
                LocalManifestEntry(
                    manifest_path="manifest-b.avro",
                    manifest_length_bytes=200,
                    content=1,
                    partition_spec_id=1,
                    added_files_count=None,
                    existing_files_count=None,
                    deleted_files_count=None,
                ),
            ],
        }
    )


def test_parity_get_manifest_stats_all_null_counts(
    tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata
) -> None:
    """If every manifest reports NULL entry counts, total_entries and the
    per-content live counts must stay NULL (not 0), matching local semantics."""
    fixture = _manifest_all_null_counts_fixture(local_table_fixture)
    rows = [
        {
            "manifest_count": 2,
            "total_manifest_size_bytes": 300,
            "total_entries": None,
            "live_data_file_entries": None,
            "live_delete_file_entries": None,
            "deleted_entries": None,
            "avg_manifest_size_bytes": 150.0,
            "avg_entries_per_manifest": None,
            "min_manifest_size_bytes": 100,
            "max_manifest_size_bytes": 200,
        }
    ]
    local_payload = _run_local(tmp_path, fixture, "get_manifest_stats", None)
    iomete_payload = _run_iomete(
        tmp_path, fixture, table_metadata, rows, "get_manifest_stats", None
    )
    assert local_payload.model_dump() == iomete_payload.model_dump()
    assert local_payload.total_entries is None
    assert local_payload.live_data_file_entries is None
    assert local_payload.live_delete_file_entries is None


def _delete_all_null_records_fixture(base: LocalTableFixture) -> LocalTableFixture:
    """Delete files where no file reports a record count."""
    return base.model_copy(
        update={
            "delete_files": [
                LocalFileEntry(
                    file_path="delete/0001.parquet",
                    file_size_bytes=4096,
                    content=1,
                    record_count=None,
                ),
                LocalFileEntry(
                    file_path="delete/0002.parquet",
                    file_size_bytes=2048,
                    content=2,
                    record_count=None,
                ),
            ],
        }
    )


def test_parity_get_delete_file_stats_all_null_records(
    tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata
) -> None:
    """If every delete file has a NULL record count, total_delete_records
    stays NULL, matching the local sum_optional contract."""
    fixture = _delete_all_null_records_fixture(local_table_fixture)
    rows = [
        {
            "delete_file_count": 2,
            "position_delete_file_count": 1,
            "equality_delete_file_count": 1,
            "total_delete_file_size_bytes": 6144,
            "total_delete_records": None,
            "min_delete_file_size_bytes": 2048,
            "median_delete_file_size_bytes": 3072.0,
            "max_delete_file_size_bytes": 4096,
        }
    ]
    local_payload = _run_local(tmp_path, fixture, "get_delete_file_stats", None)
    iomete_payload = _run_iomete(
        tmp_path, fixture, table_metadata, rows, "get_delete_file_stats", None
    )
    assert local_payload.model_dump() == iomete_payload.model_dump()
    assert local_payload.total_delete_records is None


def test_parity_get_delete_file_stats(tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata) -> None:
    rows = [
        {
            "delete_file_count": 1,
            "position_delete_file_count": 1,
            "equality_delete_file_count": 0,
            "total_delete_file_size_bytes": 4096,
            "total_delete_records": 5,
            "min_delete_file_size_bytes": 4096,
            "median_delete_file_size_bytes": 4096.0,
            "max_delete_file_size_bytes": 4096,
        }
    ]
    local_payload = _run_local(tmp_path, local_table_fixture, "get_delete_file_stats", None)
    iomete_payload = _run_iomete(tmp_path, local_table_fixture, table_metadata, rows, "get_delete_file_stats", None)
    assert local_payload.model_dump() == iomete_payload.model_dump()


def _partition_overflow_fixture(base: LocalTableFixture) -> LocalTableFixture:
    """Five distinct partitions so limit < partition_count exercises full-population stats."""
    return base.model_copy(
        update={
            "data_files": [
                LocalFileEntry(
                    file_path=f"data/{i:04d}.parquet",
                    file_size_bytes=(5 - i) * 1000,
                    content=0,
                    record_count=(5 - i) * 10,
                    partition={"created_at_day": f"2023-11-0{i + 1}"},
                    partition_spec_id=0,
                    sort_order_id=None,
                )
                for i in range(5)
            ],
            "delete_files": [],
            "column_metrics": {},
            "column_profiles": {},
        }
    )


def test_parity_get_partition_layout_more_than_limit(
    tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata
) -> None:
    fixture = _partition_overflow_fixture(local_table_fixture)
    rows = [
        {
            "partition": {"created_at_day": f"2023-11-0{i + 1}"},
            "spec_id": 0,
            "file_count": 1,
            "total_size_bytes": (5 - i) * 1000,
            "total_record_count": (5 - i) * 10,
        }
        for i in range(5)
    ]
    local_payload = _run_local(
        tmp_path, fixture, "get_partition_layout", {"limit": 2}
    )
    iomete_payload = _run_iomete(
        tmp_path, fixture, table_metadata, rows, "get_partition_layout", {"limit": 2}
    )
    assert local_payload.model_dump() == iomete_payload.model_dump()
    # Distribution statistics must reflect the full population, not only the
    # two partitions returned in entries.
    assert local_payload.partition_count == 5
    assert len(local_payload.entries) == 2
    assert local_payload.files_per_partition_min == 1
    assert local_payload.files_per_partition_median == 1.0
    assert local_payload.files_per_partition_max == 1
    assert local_payload.size_bytes_per_partition_min == 1000
    assert local_payload.size_bytes_per_partition_median == 3000.0
    assert local_payload.size_bytes_per_partition_max == 5000
    assert local_payload.largest_partition.total_size_bytes == 5000
    assert local_payload.smallest_partition.total_size_bytes == 1000


def _manifest_null_counts_fixture(base: LocalTableFixture) -> LocalTableFixture:
    """Manifests where one report has NULL entry counts; local contract keeps
    the partial sums and distinguishes all-NULL from zero."""
    return base.model_copy(
        update={
            "manifests": [
                LocalManifestEntry(
                    manifest_path="manifest-1.avro",
                    manifest_length_bytes=100,
                    content=0,
                    partition_spec_id=1,
                    added_files_count=3,
                    existing_files_count=0,
                    deleted_files_count=0,
                ),
                LocalManifestEntry(
                    manifest_path="manifest-2.avro",
                    manifest_length_bytes=200,
                    content=1,
                    partition_spec_id=1,
                    added_files_count=None,
                    existing_files_count=None,
                    deleted_files_count=None,
                ),
                LocalManifestEntry(
                    manifest_path="manifest-3.avro",
                    manifest_length_bytes=300,
                    content=0,
                    partition_spec_id=1,
                    added_files_count=1,
                    existing_files_count=2,
                    deleted_files_count=None,
                ),
            ],
        }
    )


def test_parity_get_manifest_stats_null_counts(
    tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata
) -> None:
    fixture = _manifest_null_counts_fixture(local_table_fixture)
    rows = [
        {
            "manifest_count": 3,
            "total_manifest_size_bytes": 600,
            "total_entries": 6,
            "live_data_file_entries": 6,
            "live_delete_file_entries": None,
            "deleted_entries": 0,
            "avg_manifest_size_bytes": 200.0,
            "avg_entries_per_manifest": 2.0,
            "min_manifest_size_bytes": 100,
            "max_manifest_size_bytes": 300,
        }
    ]
    local_payload = _run_local(tmp_path, fixture, "get_manifest_stats", None)
    iomete_payload = _run_iomete(
        tmp_path, fixture, table_metadata, rows, "get_manifest_stats", None
    )
    assert local_payload.model_dump() == iomete_payload.model_dump()


def _delete_null_records_fixture(base: LocalTableFixture) -> LocalTableFixture:
    """Delete files where one does not report a record count."""
    return base.model_copy(
        update={
            "delete_files": [
                LocalFileEntry(
                    file_path="delete/0001.parquet",
                    file_size_bytes=4096,
                    content=1,
                    record_count=5,
                ),
                LocalFileEntry(
                    file_path="delete/0002.parquet",
                    file_size_bytes=2048,
                    content=2,
                    record_count=None,
                ),
            ],
        }
    )


def test_parity_get_delete_file_stats_null_records(
    tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata
) -> None:
    fixture = _delete_null_records_fixture(local_table_fixture)
    rows = [
        {
            "delete_file_count": 2,
            "position_delete_file_count": 1,
            "equality_delete_file_count": 1,
            "total_delete_file_size_bytes": 6144,
            "total_delete_records": 5,
            "min_delete_file_size_bytes": 2048,
            "median_delete_file_size_bytes": 3072.0,
            "max_delete_file_size_bytes": 4096,
        }
    ]
    local_payload = _run_local(tmp_path, fixture, "get_delete_file_stats", None)
    iomete_payload = _run_iomete(
        tmp_path, fixture, table_metadata, rows, "get_delete_file_stats", None
    )
    assert local_payload.model_dump() == iomete_payload.model_dump()


def test_parity_get_snapshot_history_real_string_summaries(
    tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata
) -> None:
    """Local fixture stores Iceberg summary values as strings; IOMETE rows
    already reflect the BIGINT cast the template performs. Both backends
    converge to the same int | None contract."""
    fixture = local_table_fixture.model_copy(
        update={
            "snapshots": [
                LocalSnapshot(
                    snapshot_id=local_table_fixture.snapshot_id,
                    parent_snapshot_id=1111111111111111111,
                    timestamp_ms=1700000000000,
                    operation="append",
                    summary={
                        # Real Iceberg snapshot summaries are strings.
                        "added-data-files": "3",
                        "deleted-data-files": "0",
                        "added-records": "300",
                        "deleted-records": "0",
                    },
                ),
            ],
        }
    )
    rows = [
        {
            "snapshot_id": "9182781280348117982",
            "parent_snapshot_id": "1111111111111111111",
            "timestamp_ms": 1700000000000,
            "operation": "append",
            # After the IOMETE template casts summary strings to BIGINT.
            "added_data_files": 3,
            "removed_data_files": 0,
            "added_records": 300,
            "removed_records": 0,
        }
    ]
    local_payload = _run_local(tmp_path, fixture, "get_snapshot_history", None)
    iomete_payload = _run_iomete(
        tmp_path, fixture, table_metadata, rows, "get_snapshot_history", None
    )
    assert local_payload.model_dump() == iomete_payload.model_dump()
    assert local_payload.snapshots[0].added_data_files == 3


def test_parity_get_sort_order_usage(tmp_path, local_table_fixture: LocalTableFixture, table_metadata: TableMetadata) -> None:
    rows = [
        {"sort_order_id": None, "file_count": 1, "total_size_bytes": 10485760},
        {"sort_order_id": 2, "file_count": 2, "total_size_bytes": 20971520},
    ]
    local_payload = _run_local(tmp_path, local_table_fixture, "get_sort_order_usage", None)
    iomete_payload = _run_iomete(tmp_path, local_table_fixture, table_metadata, rows, "get_sort_order_usage", None)
    assert local_payload.model_dump() == iomete_payload.model_dump()
