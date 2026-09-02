#!/usr/bin/env python3
"""Seed the local Docker Iceberg environment with deterministic benchmark tables.

Runtime_Environments_UI.md §15, §52.

This script runs after ``docker compose up -d`` (and after ``minio-init`` has
created the warehouse bucket). It uses PyIceberg to create the ``demo``
namespace and a reproducible set of benchmark tables for STA evaluation.
No Spark is required.

Usage:
    python scripts/seed_local.py

Configuration is loaded through ``sta.config.get_settings()``, which reads
environment variables and an optional ``.env`` file exactly like the
application.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
import sys
from collections.abc import Sequence
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("seed_local")

import pyarrow as pa  # noqa: E402

from sta.config import get_settings  # noqa: E402
from sta.execution.backends.local_catalog import load_local_catalog  # noqa: E402

random.seed(42)

_NAMESPACE = "demo"


def _base_schema() -> Any:
    """Shared schema for the benchmark tables."""
    from pyiceberg.schema import Schema
    from pyiceberg.types import BooleanType, DateType, DoubleType, LongType, NestedField, StringType

    return Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "event_date", DateType(), required=False),
        NestedField(3, "category", StringType(), required=False),
        NestedField(4, "amount", DoubleType(), required=False),
        NestedField(5, "active", BooleanType(), required=False),
    )


def _orders_schema() -> Any:
    """Production-style order/event schema for partition/layout benchmarks."""
    from pyiceberg.schema import Schema
    from pyiceberg.types import DateType, DoubleType, LongType, NestedField, StringType, TimestamptzType

    return Schema(
        NestedField(1, "order_id", LongType(), required=True),
        NestedField(2, "customer_id", LongType(), required=False),
        NestedField(3, "event_timestamp", TimestamptzType(), required=False),
        NestedField(4, "event_date", DateType(), required=False),
        NestedField(5, "category", StringType(), required=False),
        NestedField(6, "status", StringType(), required=False),
        NestedField(7, "region", StringType(), required=False),
        NestedField(8, "amount", DoubleType(), required=False),
    )


def _identifier_heavy_schema() -> Any:
    """Schema dominated by identifier-like columns to exercise classification."""
    from pyiceberg.schema import Schema
    from pyiceberg.types import BooleanType, DateType, DoubleType, LongType, NestedField, StringType

    return Schema(
        NestedField(1, "id", LongType(), required=True),
        NestedField(2, "user_id", LongType(), required=False),
        NestedField(3, "order_id", LongType(), required=False),
        NestedField(4, "customer_uuid", StringType(), required=False),
        NestedField(5, "product_uuid", StringType(), required=False),
        NestedField(6, "event_guid", StringType(), required=False),
        NestedField(7, "transaction_id", LongType(), required=False),
        NestedField(8, "device_id", StringType(), required=False),
        NestedField(9, "merchant_id", LongType(), required=False),
        NestedField(10, "session_id", StringType(), required=False),
        NestedField(11, "amount", DoubleType(), required=False),
        NestedField(12, "event_date", DateType(), required=False),
        NestedField(13, "active", BooleanType(), required=False),
    )


def _drop_if_exists(catalog: Any, name: str) -> None:
    try:
        catalog.drop_table(name)
        logger.info("dropped existing table %s", name)
    except Exception:
        pass


def _create_table(
    catalog: Any,
    name: str,
    *,
    schema: Any | None = None,
    partition_spec: Any | None = None,
    sort_order: Any | None = None,
    properties: dict[str, str] | None = None,
) -> Any:
    table_schema = schema if schema is not None else _base_schema()
    _drop_if_exists(catalog, name)
    kwargs: dict[str, Any] = {"schema": table_schema, "properties": properties or {}}
    if partition_spec is not None:
        kwargs["partition_spec"] = partition_spec
    if sort_order is not None:
        kwargs["sort_order"] = sort_order
    return catalog.create_table(name, **kwargs)


def _make_batch(
    schema: Any,
    num_rows: int,
    start_id: int,
    date: dt.date,
) -> pa.Table:
    """One deterministic Arrow batch matching the shared Iceberg schema."""
    pa_schema = schema.as_arrow()
    return pa.table(
        {
            "id": list(range(start_id, start_id + num_rows)),
            "event_date": [date] * num_rows,
            "category": random.choices(["a", "b", "c"], k=num_rows),
            "amount": [random.uniform(0.0, 1000.0) for _ in range(num_rows)],
            "active": random.choices([True, False], k=num_rows),
        },
        schema=pa_schema,
    )


def _make_identifier_batch(
    schema: Any,
    num_rows: int,
    start_id: int,
    date: dt.date,
) -> pa.Table:
    """One deterministic Arrow batch for the identifier-heavy schema."""
    pa_schema = schema.as_arrow()
    ids = list(range(start_id, start_id + num_rows))
    return pa.table(
        {
            "id": ids,
            "user_id": [i + 1_000 for i in ids],
            "order_id": [i + 2_000 for i in ids],
            "customer_uuid": [f"cu_{(i % 1_000):04d}" for i in ids],
            "product_uuid": [f"pu_{(i % 500):03d}" for i in ids],
            "event_guid": [f"eg_{(i % 200):03d}" for i in ids],
            "transaction_id": [i + 3_000 for i in ids],
            "device_id": [f"d_{(i % 50):02d}" for i in ids],
            "merchant_id": [i + 4_000 for i in ids],
            "session_id": [f"s_{(i % 100):03d}" for i in ids],
            "amount": [random.uniform(0.0, 1000.0) for _ in range(num_rows)],
            "event_date": [date] * num_rows,
            "active": random.choices([True, False], k=num_rows),
        },
        schema=pa_schema,
    )


def _make_orders_batch(
    schema: Any,
    num_rows: int,
    start_id: int,
    base_date: dt.date,
    *,
    hour_offset: int = 0,
) -> pa.Table:
    """One deterministic Arrow batch for the order/event benchmark schema.

    Rows are spread across the hour starting at ``hour_offset`` hours past
    ``base_date`` so hourly partitioning creates one partition per batch.
    """
    from pyiceberg.types import TimestamptzType

    pa_schema = schema.as_arrow()
    hour_start = dt.datetime.combine(base_date, dt.time.min, tzinfo=dt.timezone.utc) + dt.timedelta(
        hours=hour_offset
    )
    ids = list(range(start_id, start_id + num_rows))
    categories = ["electronics", "apparel", "home", "sports", "books"]
    statuses = ["placed", "shipped", "delivered", "returned"]
    regions = ["na", "eu", "apac"]
    timestamps = [
        hour_start + dt.timedelta(seconds=random.randint(0, 3599))
        for _ in range(num_rows)
    ]
    return pa.table(
        {
            "order_id": ids,
            "customer_id": [i + 100_000 for i in ids],
            "event_timestamp": timestamps,
            "event_date": [base_date + dt.timedelta(days=hour_offset // 24)] * num_rows,
            "category": random.choices(categories, k=num_rows),
            "status": random.choices(statuses, k=num_rows),
            "region": random.choices(regions, k=num_rows),
            "amount": [round(random.uniform(5.0, 2_000.0), 2) for _ in range(num_rows)],
        },
        schema=pa_schema,
    )


def _make_candidate_events_batch(
    schema: Any,
    num_rows: int,
    start_id: int,
    base_date: dt.date,
    day_offset: int,
) -> pa.Table:
    """One batch for the partition-candidate benchmark table.

    Timestamps are scattered across ``day_offset`` days so the candidate
    analysis sees a realistic temporal distribution without an identifier
    column being involved.
    """
    pa_schema = schema.as_arrow()
    ids = list(range(start_id, start_id + num_rows))
    categories = ["click", "view", "purchase", "signup", "logout"]
    statuses = ["ok", "fail", "pending"]
    day_start = dt.datetime.combine(
        base_date + dt.timedelta(days=day_offset), dt.time.min, tzinfo=dt.timezone.utc
    )
    timestamps = [
        day_start + dt.timedelta(seconds=random.randint(0, 86_399))
        for _ in range(num_rows)
    ]
    return pa.table(
        {
            "event_id": ids,
            "event_timestamp": timestamps,
            "event_date": [base_date + dt.timedelta(days=day_offset)] * num_rows,
            "category": random.choices(categories, k=num_rows),
            "status": random.choices(statuses, k=num_rows),
            "amount": [round(random.uniform(1.0, 500.0), 2) for _ in range(num_rows)],
        },
        schema=pa_schema,
    )


def _make_identifier_orders_batch(
    schema: Any,
    num_rows: int,
    start_customer_id: int,
    base_date: dt.date,
) -> pa.Table:
    """One batch where every row has a distinct ``customer_id``.

    Used with an identity partition on ``customer_id`` to create thousands
    of tiny identifier-keyed partitions.
    """
    pa_schema = schema.as_arrow()
    customer_ids = list(range(start_customer_id, start_customer_id + num_rows))
    timestamp = dt.datetime.combine(base_date, dt.time(12, 0, 0), tzinfo=dt.timezone.utc)
    return pa.table(
        {
            "order_id": customer_ids,
            "customer_id": customer_ids,
            "account_uuid": [f"acct_{i:08d}" for i in customer_ids],
            "transaction_guid": [f"txn_{i:08d}" for i in customer_ids],
            "product_id": [i % 100 for i in customer_ids],
            "merchant_id": [i % 50 for i in customer_ids],
            "session_id": [f"sess_{(i % 1_000):04d}" for i in customer_ids],
            "device_id": [f"dev_{(i % 20):02d}" for i in customer_ids],
            "event_timestamp": [timestamp] * num_rows,
            "amount": [round(random.uniform(10.0, 1_000.0), 2) for _ in range(num_rows)],
            "event_date": [base_date] * num_rows,
        },
        schema=pa_schema,
    )


def _healthy_table(catalog: Any) -> None:
    """Single snapshot, unpartitioned, one reasonably-sized data file."""
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.healthy_table",
        sort_order=_sort_order([(1, "asc")]),
    )
    batch = _make_batch(table.metadata.schemas[0], 10_000, 1, dt.date(2026, 1, 1))
    table.append(batch)
    logger.info("created %s (%d rows)", table.name(), batch.num_rows)


def _small_files_table(catalog: Any) -> None:
    """Many tiny files in a single snapshot (small-file accumulation)."""
    table = _create_table(catalog, f"{_NAMESPACE}.small_files_table")
    schema = table.metadata.schemas[0]
    for i in range(100):
        batch = _make_batch(schema, 10, 1 + i * 10, dt.date(2026, 1, 1))
        table.append(batch)
    logger.info("created %s (%d appends)", table.name(), 100)


def _partition_fragmentation_table(catalog: Any) -> None:
    """Partitioned by day with a few rows per day -> many small partitions."""
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import DayTransform

    partition_spec = PartitionSpec(
        PartitionField(source_id=2, field_id=1000, transform=DayTransform(), name="event_day")
    )
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.partition_fragmentation_table",
        partition_spec=partition_spec,
    )
    schema = table.metadata.schemas[0]
    base = dt.date(2026, 1, 1)
    start_id = 1
    for day in range(90):
        date = base + dt.timedelta(days=day)
        batch = _make_batch(schema, 20, start_id, date)
        table.append(batch)
        start_id += 20
    logger.info("created %s (90 days)", table.name())


def _delete_files_unsupported_table(catalog: Any) -> None:
    """Append data then delete a slice.

    PyIceberg's local DELETE implementation currently rewrites data files
    (copy-on-write) rather than writing Iceberg position/equality delete
    files. The resulting snapshot reports ``total-delete-files=0``. This
    benchmark is therefore *not* a delete-files test; it is an explicit
    unsupported-capability marker showing that the local backend reports
    zero delete files truthfully instead of faking unsupported metadata.
    """
    table = _create_table(catalog, f"{_NAMESPACE}.delete_files_unsupported_table")
    schema = table.metadata.schemas[0]
    batch = _make_batch(schema, 2_000, 1, dt.date(2026, 1, 1))
    table.append(batch)
    try:
        table.delete("id < 500")
        logger.info("created %s (PyIceberg copy-on-write delete)", table.name())
    except Exception as exc:
        logger.warning("could not apply delete for %s: %s", table.name(), type(exc).__name__)


def _snapshot_growth_table(catalog: Any) -> None:
    """Several appends, each in its own snapshot."""
    table = _create_table(catalog, f"{_NAMESPACE}.snapshot_growth_table")
    schema = table.metadata.schemas[0]
    base = dt.date(2026, 1, 1)
    start_id = 1
    for snapshot in range(5):
        batch = _make_batch(schema, 1_000, start_id, base + dt.timedelta(days=snapshot))
        table.append(batch)
        start_id += 1_000
    logger.info("created %s (%d snapshots)", table.name(), 5)


def _no_sort_order_table(catalog: Any) -> None:
    """Unpartitioned, no sort order, moderate size."""
    table = _create_table(catalog, f"{_NAMESPACE}.no_sort_order_table")
    schema = table.metadata.schemas[0]
    batch = _make_batch(schema, 5_000, 1, dt.date(2026, 1, 1))
    table.append(batch)
    logger.info("created %s (%d rows)", table.name(), batch.num_rows)


def _identifier_heavy_table(catalog: Any) -> None:
    """Schema dominated by identifier-like columns.

    Exercises the identifier-like column classifier and context builder
    without a real partition-candidate query on an identifier column.
    """
    schema = _identifier_heavy_schema()
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.identifier_heavy_table",
        schema=schema,
    )
    batch = _make_identifier_batch(schema, 1_000, 1, dt.date(2026, 1, 1))
    table.append(batch)
    logger.info("created %s (%d columns, %d rows)", table.name(), len(schema.fields), batch.num_rows)


def _missing_metrics_table(catalog: Any) -> None:
    """Table written with metrics disabled.

    Iceberg metadata carries the data file but no per-column value counts,
    null counts, or bounds. This exercises the tool contract for missing
    metrics / insufficient evidence.
    """
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.missing_metrics_table",
        properties={"write.metadata.metrics.default": "none"},
    )
    schema = table.metadata.schemas[0]
    batch = _make_batch(schema, 1_000, 1, dt.date(2026, 1, 1))
    table.append(batch)
    logger.info("created %s (%d rows, metrics disabled)", table.name(), batch.num_rows)


def _multiple_issues_table(catalog: Any) -> None:
    """Several low-severity problems at once: small files, no sort order,
    many snapshots."""
    table = _create_table(catalog, f"{_NAMESPACE}.multiple_issues_table")
    schema = table.metadata.schemas[0]
    for i in range(50):
        batch = _make_batch(schema, 20, 1 + i * 20, dt.date(2026, 1, 1))
        table.append(batch)
    logger.info("created %s (%d appends)", table.name(), 50)


def _small_files_not_material_table(catalog: Any) -> None:
    """Tiny table with small files that are not a practical concern.

    Files are small, but the total table is only a few kilobytes, so a
    compaction recommendation would not be material.
    """
    table = _create_table(catalog, f"{_NAMESPACE}.small_files_not_material_table")
    schema = table.metadata.schemas[0]
    for i in range(5):
        batch = _make_batch(schema, 5, 1 + i * 5, dt.date(2026, 1, 1))
        table.append(batch)
    logger.info("created %s (%d tiny appends)", table.name(), 5)


def _orders_bad_spec_table(catalog: Any) -> None:
    """Large-ish event table with a deliberately poor partition spec.

    Partitioned by ``hours(event_timestamp)`` over two weeks, every hourly
    append creates one more tiny file and one more snapshot. The resulting
    table has hundreds of small partitions, hundreds of snapshots/manifests,
    and realistic writer properties that the data volume clearly violates.
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import HourTransform

    schema = _orders_schema()
    partition_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=HourTransform(), name="event_hour")
    )
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.orders_bad_spec",
        schema=schema,
        partition_spec=partition_spec,
        properties={
            "write.target-file-size-bytes": "134217728",
            "write.parquet.row-group-size-bytes": "8388608",
            "write.metadata.metrics.default": "full",
            "write.metadata.previous-versions-max": "10",
        },
    )
    schema = table.metadata.schemas[0]
    base = dt.date(2026, 1, 1)
    rows_per_hour = 300
    start_id = 1
    hours = 14 * 24
    for hour in range(hours):
        batch = _make_orders_batch(
            schema, rows_per_hour, start_id, base, hour_offset=hour
        )
        table.append(batch)
        start_id += rows_per_hour
    logger.info("created %s (%d hourly appends, %d rows)", table.name(), hours, start_id - 1)


def _orders_bad_spec_caps_properties_table(catalog: Any) -> None:
    """Casing test: bad partition spec + inert UPPERCASE custom properties.

    Same deliberately poor ``hours(event_timestamp)`` spec as
    ``orders_bad_spec``, but the writer configuration is deliberately broken:
    the settings a user might write with uppercase keys
    (``WRITE.TARGET-FILE-SIZE-BYTES``, ``WRITE.PARQUET.ROW-GROUP-SIZE-BYTES``,
    ``WRITE.METADATA.METRICS.DEFAULT``) are **not** Iceberg writer settings.
    Iceberg property keys are case-sensitive and engines only recognize the
    lowercase writer keys, so these uppercase keys are preserved verbatim as
    inert custom table metadata and control nothing.

    Truthful facts this table must demonstrate:

    - the uppercase keys round-trip through Iceberg metadata and STA's
      ``TableMetadata.properties`` byte-for-byte (values included),
    - they are surfaced verbatim in the compact TableContext's relevant
      properties with their raw uppercase keys (known keys are recognized
      case-insensitively, never normalized), so the Investigator can see the
      inert variants without mistaking them for effective writer
      configuration; only exact lowercase keys drive derived facts such as
      metrics availability,
    - the only effective writer property is the standard lowercase
      ``write.metadata.metrics.default=full`` (kept because the benchmark's
      metadata-metrics tooling needs real per-file counts and bounds); actual
      file sizing stayed at the engine default, which the tiny measured files
      confirm,
    - the measured layout facts (files, snapshots, partitions) come from the
      real writes and are independent of the inert keys.
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import HourTransform

    schema = _orders_schema()
    partition_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=HourTransform(), name="event_hour")
    )
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.orders_bad_spec_caps_properties",
        schema=schema,
        partition_spec=partition_spec,
        properties={
            # Deliberately non-effective custom metadata: uppercase keys are
            # not Iceberg writer keys and are honored by no engine.
            "WRITE.TARGET-FILE-SIZE-BYTES": "134217728",
            "WRITE.PARQUET.ROW-GROUP-SIZE-BYTES": "8388608",
            "WRITE.METADATA.METRICS.DEFAULT": "none",
            # The only effective writer property: standard lowercase key that
            # makes per-file column counts/bounds available to the tools.
            "write.metadata.metrics.default": "full",
        },
    )
    schema = table.metadata.schemas[0]
    base = dt.date(2026, 1, 1)
    rows_per_hour = 200
    start_id = 1
    hours = 3 * 24
    for hour in range(hours):
        batch = _make_orders_batch(schema, rows_per_hour, start_id, base, hour_offset=hour)
        table.append(batch)
        start_id += rows_per_hour
    logger.info(
        "created %s (%d hourly appends, %d rows, uppercase keys inert)",
        table.name(),
        hours,
        start_id - 1,
    )


def _orders_day_partitioned_table(catalog: Any) -> None:
    """Same order schema with a reasonable temporal partition and healthier layout."""
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.transforms import DayTransform

    schema = _orders_schema()
    partition_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=DayTransform(), name="event_day")
    )
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.orders_day_partitioned",
        schema=schema,
        partition_spec=partition_spec,
        sort_order=_sort_order([(3, "asc")]),
        properties={
            "write.target-file-size-bytes": "268435456",
            "write.parquet.row-group-size-bytes": "8388608",
            "write.metadata.metrics.default": "full",
        },
    )
    schema = table.metadata.schemas[0]
    base = dt.date(2026, 1, 1)
    rows_per_day = 10_000
    start_id = 1
    days = 30
    for day in range(days):
        batch = _make_orders_batch(
            schema, rows_per_day, start_id, base + dt.timedelta(days=day), hour_offset=0
        )
        table.append(batch)
        start_id += rows_per_day
    logger.info("created %s (%d daily appends, %d rows)", table.name(), days, start_id - 1)


def _events_partition_candidate_table(catalog: Any) -> None:
    """Unpartitioned event table with enough temporal distribution for
    targeted non-identifier partition analysis."""
    from pyiceberg.schema import Schema
    from pyiceberg.types import DateType, DoubleType, LongType, NestedField, StringType, TimestamptzType

    schema = Schema(
        NestedField(1, "event_id", LongType(), required=True),
        NestedField(2, "event_timestamp", TimestamptzType(), required=False),
        NestedField(3, "event_date", DateType(), required=False),
        NestedField(4, "category", StringType(), required=False),
        NestedField(5, "status", StringType(), required=False),
        NestedField(6, "amount", DoubleType(), required=False),
    )
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.events_partition_candidate",
        schema=schema,
        properties={
            "write.target-file-size-bytes": "67108864",
            "write.metadata.metrics.default": "full",
        },
    )
    schema = table.metadata.schemas[0]
    base = dt.date(2026, 1, 1)
    rows_per_batch = 5_000
    batches = 10
    start_id = 1
    for day_offset in range(batches):
        batch = _make_candidate_events_batch(
            schema, rows_per_batch, start_id, base, day_offset
        )
        table.append(batch)
        start_id += rows_per_batch
    logger.info("created %s (%d batches, %d rows)", table.name(), batches, start_id - 1)


def _customer_orders_identifier_heavy_table(catalog: Any) -> None:
    """Identifier-heavy table with a deliberately poor identity partition.

    ``customer_id`` is identity-partitioned, so a modest number of rows
    becomes thousands of tiny partitions/files and expensive candidate
    analysis on any *_id/uuid/guid column must be rejected.
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import DateType, DoubleType, LongType, NestedField, StringType, TimestamptzType

    schema = Schema(
        NestedField(1, "order_id", LongType(), required=True),
        NestedField(2, "customer_id", LongType(), required=False),
        NestedField(3, "account_uuid", StringType(), required=False),
        NestedField(4, "transaction_guid", StringType(), required=False),
        NestedField(5, "product_id", LongType(), required=False),
        NestedField(6, "merchant_id", LongType(), required=False),
        NestedField(7, "session_id", StringType(), required=False),
        NestedField(8, "device_id", StringType(), required=False),
        NestedField(9, "event_timestamp", TimestamptzType(), required=False),
        NestedField(10, "amount", DoubleType(), required=False),
        NestedField(11, "event_date", DateType(), required=False),
    )
    partition_spec = PartitionSpec(
        PartitionField(source_id=2, field_id=1000, transform=IdentityTransform(), name="customer_id")
    )
    table = _create_table(
        catalog,
        f"{_NAMESPACE}.customer_orders_identifier_heavy",
        schema=schema,
        partition_spec=partition_spec,
        properties={
            "write.target-file-size-bytes": "134217728",
            "write.metadata.metrics.default": "full",
        },
    )
    schema = table.metadata.schemas[0]
    base = dt.date(2026, 1, 1)
    rows_per_batch = 1_000
    batches = 2
    start_customer_id = 1
    for _ in range(batches):
        batch = _make_identifier_orders_batch(
            schema, rows_per_batch, start_customer_id, base
        )
        table.append(batch)
        start_customer_id += rows_per_batch
    logger.info(
        "created %s (%d distinct customers, %d batches)",
        table.name(),
        start_customer_id - 1,
        batches,
    )


def _wide_schema_table(catalog: Any) -> None:
    """Many columns to exercise schema-map / context handling."""
    from pyiceberg.schema import Schema
    from pyiceberg.types import DoubleType, LongType, NestedField, StringType

    fields = [NestedField(1, "id", LongType(), required=True)]
    for i in range(50):
        if i % 3 == 0:
            fields.append(NestedField(i + 2, f"col_{i}_str", StringType(), required=False))
        else:
            fields.append(NestedField(i + 2, f"col_{i}_num", DoubleType(), required=False))
    schema = Schema(*fields)
    _drop_if_exists(catalog, f"{_NAMESPACE}.wide_schema_table")
    table = catalog.create_table(f"{_NAMESPACE}.wide_schema_table", schema=schema)
    pa_schema = schema.as_arrow()
    columns: dict[str, Sequence[Any]] = {"id": list(range(1_000))}
    for field in fields[1:]:
        if isinstance(field.field_type, StringType):
            columns[field.name] = [f"v_{i % 100}" for i in range(1_000)]
        else:
            columns[field.name] = [random.uniform(0.0, 1000.0) for _ in range(1_000)]
    table.append(pa.table(columns, schema=pa_schema))
    logger.info("created %s (%d columns, %d rows)", table.name(), len(fields), 1_000)


def _sort_order(fields: Sequence[tuple[int, str]]) -> Any:
    """Build a SortOrder from (source_id, direction) pairs."""
    from pyiceberg.table.sorting import NullOrder, SortDirection, SortField, SortOrder
    from pyiceberg.transforms import IdentityTransform

    def _direction(value: str) -> SortDirection:
        return SortDirection.ASC if value.lower() == "asc" else SortDirection.DESC

    return SortOrder(
        order_id=1,
        fields=[
            SortField(
                source_id=source_id,
                transform=IdentityTransform(),
                direction=_direction(direction),
                null_order=NullOrder.NULLS_FIRST,
            )
            for source_id, direction in fields
        ],
    )


def _ensure_namespace(catalog: Any) -> None:
    try:
        catalog.create_namespace(_NAMESPACE)
        logger.info("created namespace %s", _NAMESPACE)
    except Exception:
        logger.info("namespace %s already exists", _NAMESPACE)


def seed(catalog: Any) -> list[str]:
    """Create all benchmark tables and return their names."""
    _ensure_namespace(catalog)
    _healthy_table(catalog)
    _small_files_table(catalog)
    _partition_fragmentation_table(catalog)
    _delete_files_unsupported_table(catalog)
    _snapshot_growth_table(catalog)
    _no_sort_order_table(catalog)
    _identifier_heavy_table(catalog)
    _missing_metrics_table(catalog)
    _multiple_issues_table(catalog)
    _small_files_not_material_table(catalog)
    _wide_schema_table(catalog)
    _orders_bad_spec_table(catalog)
    _orders_bad_spec_caps_properties_table(catalog)
    _orders_day_partitioned_table(catalog)
    _events_partition_candidate_table(catalog)
    _customer_orders_identifier_heavy_table(catalog)
    return [
        f"{_NAMESPACE}.healthy_table",
        f"{_NAMESPACE}.small_files_table",
        f"{_NAMESPACE}.partition_fragmentation_table",
        f"{_NAMESPACE}.delete_files_unsupported_table",
        f"{_NAMESPACE}.snapshot_growth_table",
        f"{_NAMESPACE}.no_sort_order_table",
        f"{_NAMESPACE}.identifier_heavy_table",
        f"{_NAMESPACE}.missing_metrics_table",
        f"{_NAMESPACE}.multiple_issues_table",
        f"{_NAMESPACE}.small_files_not_material_table",
        f"{_NAMESPACE}.wide_schema_table",
        f"{_NAMESPACE}.orders_bad_spec",
        f"{_NAMESPACE}.orders_bad_spec_caps_properties",
        f"{_NAMESPACE}.orders_day_partitioned",
        f"{_NAMESPACE}.events_partition_candidate",
        f"{_NAMESPACE}.customer_orders_identifier_heavy",
    ]


def main() -> int:
    settings = get_settings()
    try:
        settings.validate_environment()
    except ValueError as exc:
        logger.error("local configuration is incomplete: %s", exc)
        return 1

    try:
        catalog = load_local_catalog(settings)
    except Exception as exc:
        logger.error("cannot connect to the local Iceberg catalog: %s", exc)
        return 1

    created = seed(catalog)
    print("\nCreated benchmark tables:")
    for name in created:
        print(f"  {name}")
    print("\nStart the app and analyze one of these tables:")
    print(f"  uvicorn sta.app.api:app --reload")
    return 0


if __name__ == "__main__":
    sys.exit(main())
