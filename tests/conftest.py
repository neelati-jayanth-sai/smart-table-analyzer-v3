"""Shared pytest fixtures for contract, execution, app, and local Docker tests."""

from pathlib import Path

import pytest

from sta.config import get_settings
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
    LocalColumnProfile,
    LocalFileEntry,
    LocalManifestEntry,
    LocalSnapshot,
    LocalTableFixture,
)
from sta.execution.backends.local_catalog import load_local_catalog
from scripts.seed_local import seed

TABLE = "demo.sales.orders"
SNAPSHOT_ID = 9182781280348117982
REPO_ROOT = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def seeded_catalog():
    """Seed the local catalog once for all Docker-local benchmark assertions.

    Skips cleanly when the local Iceberg environment is not configured or not
    reachable. Any test that needs the seeded tables uses this fixture.
    """
    settings = get_settings()
    try:
        settings.validate_environment()
    except ValueError as exc:
        pytest.skip(f"local Iceberg configuration is not available: {exc}")
    try:
        catalog = load_local_catalog(settings)
    except Exception as exc:  # pragma: no cover - Docker may not be running
        pytest.skip(f"local Iceberg catalog is not reachable: {exc}")
    seed(catalog)
    return catalog


@pytest.fixture
def local_table_fixture() -> LocalTableFixture:
    """Deterministic Iceberg-like fixture with two partition specs and varied files."""
    return LocalTableFixture(
        table=TABLE,
        snapshot_id=SNAPSHOT_ID,
        schema_fields=[
            SchemaField(field_id=1, name="order_id", type="long", required=True),
            SchemaField(field_id=2, name="customer_id", type="long", required=True),
            SchemaField(field_id=3, name="created_at", type="timestamptz", required=True),
            SchemaField(field_id=4, name="status", type="string", required=True),
            SchemaField(field_id=5, name="amount", type="double", required=False),
        ],
        snapshots=[
            LocalSnapshot(
                snapshot_id=SNAPSHOT_ID,
                parent_snapshot_id=1111111111111111111,
                timestamp_ms=1700000000000,
                operation="append",
                summary={
                    "added-data-files": "3",
                    "deleted-data-files": "0",
                    "added-records": "300",
                    "deleted-records": "0",
                    "total-data-files": "3",
                    "total-files-size": "31457280",
                    "total-records": "300",
                },
            ),
            LocalSnapshot(
                snapshot_id=2222222222222222222,
                parent_snapshot_id=None,
                timestamp_ms=1699000000000,
                operation="append",
                summary={
                    "added-data-files": "1",
                    "deleted-data-files": "0",
                    "added-records": "100",
                    "deleted-records": "0",
                    "total-data-files": "1",
                    "total-files-size": "10485760",
                    "total-records": "100",
                },
            ),
        ],
        manifests=[
            LocalManifestEntry(
                manifest_path="manifest-1.avro",
                manifest_length_bytes=1234,
                content=0,
                partition_spec_id=1,
                added_files_count=3,
                existing_files_count=0,
                deleted_files_count=0,
            ),
        ],
        data_files=[
            LocalFileEntry(
                file_path="data/0001.parquet",
                file_size_bytes=10485760,
                content=0,
                record_count=100,
                partition={"created_at_day": "2023-11-01", "order_bucket": "5"},
                partition_spec_id=1,
                sort_order_id=2,
            ),
            LocalFileEntry(
                file_path="data/0002.parquet",
                file_size_bytes=10485760,
                content=0,
                record_count=100,
                partition={"created_at_day": "2023-11-01", "order_bucket": "12"},
                partition_spec_id=1,
                sort_order_id=2,
            ),
            LocalFileEntry(
                file_path="data/0003.parquet",
                file_size_bytes=10485760,
                content=0,
                record_count=100,
                partition={"created_at_day": "2023-11-02", "order_bucket": "3"},
                partition_spec_id=1,
                sort_order_id=None,
            ),
        ],
        delete_files=[
            LocalFileEntry(
                file_path="delete/0001.parquet",
                file_size_bytes=4096,
                content=1,
                record_count=5,
            ),
        ],
        column_metrics={
            "created_at": [
                LocalColumnMetrics(
                    file_path="data/0001.parquet",
                    value_count=100,
                    null_count=0,
                    nan_count=0,
                    lower_bound="2023-11-01T00:00:00",
                    upper_bound="2023-11-01T23:59:59",
                ),
                LocalColumnMetrics(
                    file_path="data/0002.parquet",
                    value_count=100,
                    null_count=0,
                    nan_count=0,
                    lower_bound="2023-11-01T00:00:00",
                    upper_bound="2023-11-01T23:59:59",
                ),
                LocalColumnMetrics(
                    file_path="data/0003.parquet",
                    value_count=100,
                    null_count=0,
                    nan_count=0,
                    lower_bound="2023-11-02T00:00:00",
                    upper_bound="2023-11-02T23:59:59",
                ),
            ],
        },
        column_profiles={
            "created_at": LocalColumnProfile(
                total_value_count=300,
                null_count=0,
                nan_count=None,
                distinct_count=2,
                min_value="2023-11-01T00:00:00",
                max_value="2023-11-02T23:59:59",
            ),
        },
        partition_specs=[
            PartitionSpec(
                spec_id=0,
                fields=[
                    PartitionField(
                        name="created_at_day",
                        transform="identity",
                        source_id=3,
                        field_id=1000,
                    )
                ],
            ),
            PartitionSpec(
                spec_id=1,
                fields=[
                    PartitionField(
                        name="created_at_day",
                        transform="day",
                        source_id=3,
                        field_id=1001,
                    ),
                    PartitionField(
                        name="order_bucket",
                        transform="bucket[16]",
                        source_id=1,
                        field_id=1002,
                    ),
                ],
            ),
        ],
        sort_orders=[
            SortOrder(order_id=1, fields=[]),
            SortOrder(
                order_id=2,
                fields=[
                    SortField(
                        transform="identity",
                        direction="asc",
                        null_order="nulls-first",
                        source_id=3,
                    ),
                ],
            ),
        ],
        default_spec_id=1,
        default_sort_order_id=2,
    )


@pytest.fixture
def status_partitioned_fixture() -> LocalTableFixture:
    """Deterministic fixture for a bad current partition (identity(status))
    with a realistic temporal alternative candidate (days(event_timestamp)).

    12 data files: 3 days x 4 statuses. Current spec creates 4 huge partitions
    (one per status, 3 files/300 records each); candidate days(event_timestamp)
    would create 3 time-bounded partitions (4 files/400 records each).
    Per-file metadata for event_timestamp is deliberately incomplete so the
    metadata-first workflow falls through to targeted analysis.
    """
    base = "2023-11-01"
    statuses = ["placed", "shipped", "delivered", "returned"]
    data_files: list[LocalFileEntry] = []
    column_metrics: list[LocalColumnMetrics] = []
    file_index = 0
    for day_offset, day in enumerate([base, "2023-11-02", "2023-11-03"]):
        for status in statuses:
            file_path = f"data/status_{status}_{day}.parquet"
            data_files.append(
                LocalFileEntry(
                    file_path=file_path,
                    file_size_bytes=10485760,
                    content=0,
                    record_count=100,
                    partition={"status": status},
                    partition_spec_id=1,
                    sort_order_id=None,
                )
            )
            # Only the first 4 files carry bounds so metadata is insufficient.
            lower = f"{day}T00:00:00" if file_index < 4 else None
            upper = f"{day}T23:59:59" if file_index < 4 else None
            column_metrics.append(
                LocalColumnMetrics(
                    file_path=file_path,
                    value_count=100,
                    null_count=0,
                    nan_count=0,
                    lower_bound=lower,
                    upper_bound=upper,
                )
            )
            file_index += 1

    top_values = [
        {"value": f"{day}T00:00:00", "file_count": 4, "record_count": 400}
        for day in [base, "2023-11-02", "2023-11-03"]
    ]

    return LocalTableFixture(
        table="local.demo.events_status_partitioned",
        snapshot_id=SNAPSHOT_ID,
        schema_fields=[
            SchemaField(field_id=1, name="event_id", type="long", required=True),
            SchemaField(field_id=2, name="event_timestamp", type="timestamptz", required=True),
            SchemaField(field_id=3, name="event_date", type="date", required=False),
            SchemaField(field_id=4, name="category", type="string", required=False),
            SchemaField(field_id=5, name="status", type="string", required=False),
            SchemaField(field_id=6, name="region", type="string", required=False),
            SchemaField(field_id=7, name="amount", type="double", required=False),
        ],
        data_files=data_files,
        column_metrics={"event_timestamp": column_metrics},
        column_profiles={
            "event_timestamp": LocalColumnProfile(
                total_value_count=1200,
                null_count=0,
                nan_count=None,
                distinct_count=3,
                min_value=f"{base}T00:00:00",
                max_value="2023-11-03T23:59:59",
                files_per_distinct_value_min=4,
                files_per_distinct_value_median=4.0,
                files_per_distinct_value_max=4,
                records_per_distinct_value_min=400,
                records_per_distinct_value_median=400.0,
                records_per_distinct_value_max=400,
                top_values=top_values,
            ),
        },
        partition_specs=[
            PartitionSpec(
                spec_id=0,
                fields=[],
            ),
            PartitionSpec(
                spec_id=1,
                fields=[
                    PartitionField(
                        name="status",
                        transform="identity",
                        source_id=5,
                        field_id=1000,
                    )
                ],
            ),
        ],
        default_spec_id=1,
        sort_orders=[SortOrder(order_id=1, fields=[])],
        default_sort_order_id=1,
    )


@pytest.fixture
def status_table_metadata() -> TableMetadata:
    """Backend-independent metadata matching status_partitioned_fixture."""
    return TableMetadata(
        table="local.demo.events_status_partitioned",
        schemas=[
            TableSchema(
                schema_id=0,
                fields=[
                    SchemaField(field_id=1, name="event_id", type="long"),
                    SchemaField(field_id=2, name="event_timestamp", type="timestamptz"),
                    SchemaField(field_id=3, name="event_date", type="date"),
                    SchemaField(field_id=4, name="category", type="string"),
                    SchemaField(field_id=5, name="status", type="string"),
                    SchemaField(field_id=6, name="region", type="string"),
                    SchemaField(field_id=7, name="amount", type="double"),
                ],
            )
        ],
        current_schema_id=0,
        current_snapshot_id=SNAPSHOT_ID,
        partition_specs=[
            PartitionSpec(
                spec_id=0,
                fields=[],
            ),
            PartitionSpec(
                spec_id=1,
                fields=[
                    PartitionField(
                        name="status",
                        transform="identity",
                        source_id=5,
                        field_id=1000,
                    )
                ],
            ),
        ],
        default_spec_id=1,
        sort_orders=[SortOrder(order_id=1, fields=[])],
        default_sort_order_id=1,
    )


@pytest.fixture
def table_metadata() -> TableMetadata:
    """Backend-independent metadata matching local_table_fixture."""
    return TableMetadata(
        table=TABLE,
        schemas=[
            TableSchema(
                schema_id=0,
                fields=[
                    SchemaField(field_id=1, name="order_id", type="long"),
                    SchemaField(field_id=2, name="customer_id", type="long"),
                    SchemaField(field_id=3, name="created_at", type="timestamptz"),
                    SchemaField(field_id=4, name="status", type="string"),
                    SchemaField(field_id=5, name="amount", type="double"),
                ],
            )
        ],
        current_schema_id=0,
        current_snapshot_id=SNAPSHOT_ID,
        partition_specs=[
            PartitionSpec(
                spec_id=0,
                fields=[
                    PartitionField(
                        name="created_at_day",
                        transform="identity",
                        source_id=3,
                        field_id=1000,
                    )
                ],
            ),
            PartitionSpec(
                spec_id=1,
                fields=[
                    PartitionField(
                        name="created_at_day",
                        transform="day",
                        source_id=3,
                        field_id=1001,
                    ),
                    PartitionField(
                        name="order_bucket",
                        transform="bucket[16]",
                        source_id=1,
                        field_id=1002,
                    ),
                ],
            ),
        ],
        default_spec_id=1,
        sort_orders=[
            SortOrder(order_id=1, fields=[]),
            SortOrder(
                order_id=2,
                fields=[
                    SortField(
                        transform="identity",
                        direction="asc",
                        null_order="nulls-first",
                        source_id=3,
                    )
                ],
            ),
        ],
        default_sort_order_id=2,
    )
