"""Docker-local benchmark table verification (Runtime_Environments_UI.md #15, #52).

Seeds the configured local Iceberg catalog with the deterministic benchmark
tables from ``scripts.seed_local`` and asserts the resulting Iceberg metadata
and local tool outputs. These tests need a running Docker compose stack; they
skip cleanly when the local catalog is not available.
"""

from __future__ import annotations

import pytest

from sta.context.schema_map import is_identifier_like
from sta.execution.backends.local import LocalIcebergBackend
from sta.execution.backends.pyiceberg_adapter import table_fixture_from_pyiceberg


def _fixture(seeded_catalog, name: str):
    py_table = seeded_catalog.load_table(name)
    return table_fixture_from_pyiceberg(py_table)


def _backend(seeded_catalog, name: str) -> tuple[LocalIcebergBackend, object]:
    fixture = _fixture(seeded_catalog, name)
    return LocalIcebergBackend(fixture), fixture


# ---------------------------------------------------------------------------
# healthy baseline
# ---------------------------------------------------------------------------


def test_healthy_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.healthy_table")
    assert len(fixture.snapshots) == 1
    assert len(fixture.data_files) == 1
    assert fixture.data_files[0].record_count == 10_000

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 1
    assert layout["total_record_count"] == 10_000


# ---------------------------------------------------------------------------
# small files (material accumulation)
# ---------------------------------------------------------------------------


def test_small_files_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.small_files_table")
    assert len(fixture.snapshots) == 100
    assert len(fixture.data_files) == 100

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 100
    assert layout["total_record_count"] == 1000
    # Each append is tiny; total table is still far below any practical target.
    assert layout["total_size_bytes"] < 500_000


# ---------------------------------------------------------------------------
# partition fragmentation
# ---------------------------------------------------------------------------


def test_partition_fragmentation_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.partition_fragmentation_table")
    assert len(fixture.data_files) == 90

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 90
    assert layout["total_record_count"] == 1800

    partition_layout = backend.execute(
        "get_partition_layout", {}, str(fixture.snapshot_id)
    ).rows
    assert len(partition_layout) == 90


# ---------------------------------------------------------------------------
# delete files unsupported marker (root defect fix)
# ---------------------------------------------------------------------------


def test_delete_files_unsupported_table(seeded_catalog):
    """PyIceberg rewrites data on delete (copy-on-write); metadata must
    report zero delete files and the local backend must report that truthfully.
    """
    backend, fixture = _backend(seeded_catalog, "demo.delete_files_unsupported_table")
    assert len(fixture.snapshots) == 2
    assert len(fixture.data_files) == 1
    assert len(fixture.delete_files) == 0
    assert all(m.content == 0 for m in fixture.manifests)

    # Second snapshot is an overwrite, not a merge-on-read delete.
    overwrite_snapshot = next(
        s for s in fixture.snapshots if s.operation == "overwrite"
    )
    assert overwrite_snapshot.summary.get("total-delete-files") == "0"
    assert overwrite_snapshot.summary.get("total-position-deletes") == "0"
    assert overwrite_snapshot.summary.get("total-equality-deletes") == "0"

    # The rewritten file holds the survivors of id < 500.
    assert fixture.data_files[0].record_count == 1501

    deletes = backend.execute(
        "get_delete_file_stats", {}, str(fixture.snapshot_id)
    ).rows[0]
    assert deletes["delete_file_count"] == 0
    assert deletes["position_delete_file_count"] == 0
    assert deletes["equality_delete_file_count"] == 0


# ---------------------------------------------------------------------------
# snapshot growth
# ---------------------------------------------------------------------------


def test_snapshot_growth_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.snapshot_growth_table")
    assert len(fixture.snapshots) == 5
    assert len(fixture.data_files) == 5

    history = backend.execute("get_snapshot_history", {}, None).rows
    assert len(history) == 5
    assert all(row["operation"] == "append" for row in history)


# ---------------------------------------------------------------------------
# no sort order
# ---------------------------------------------------------------------------


def test_no_sort_order_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.no_sort_order_table")
    assert len(fixture.snapshots) == 1
    assert len(fixture.data_files) == 1
    assert fixture.data_files[0].sort_order_id is None
    assert fixture.default_sort_order_id is None or fixture.default_sort_order_id == 0


# ---------------------------------------------------------------------------
# identifier-heavy schema
# ---------------------------------------------------------------------------


def test_identifier_heavy_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.identifier_heavy_table")
    assert len(fixture.schema_fields) == 13
    identifier_names = [
        f.name for f in fixture.schema_fields if is_identifier_like(f.name)
    ]
    assert "id" in identifier_names
    assert "user_id" in identifier_names
    assert "order_id" in identifier_names
    assert "customer_uuid" in identifier_names
    assert "event_guid" in identifier_names
    assert len(identifier_names) >= 10


# ---------------------------------------------------------------------------
# missing metrics / insufficient evidence
# ---------------------------------------------------------------------------


def test_missing_metrics_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.missing_metrics_table")
    assert len(fixture.data_files) == 1
    assert not fixture.column_metrics  # no per-column metrics recorded

    metrics = backend.execute(
        "get_column_metadata_metrics", {"column": "id"}, str(fixture.snapshot_id)
    ).rows[0]
    assert metrics["files_measured"] == 1
    assert metrics["files_with_value_counts"] == 0
    assert metrics["files_with_bounds"] == 0
    assert metrics["value_count_sum"] is None
    assert metrics["lower_bound"] is None
    assert metrics["upper_bound"] is None


# ---------------------------------------------------------------------------
# multiple issues
# ---------------------------------------------------------------------------


def test_multiple_issues_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.multiple_issues_table")
    assert len(fixture.snapshots) == 50
    assert len(fixture.data_files) == 50
    assert fixture.default_sort_order_id in (None, 0)
    assert all(f.sort_order_id is None for f in fixture.data_files)

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 50
    assert layout["total_record_count"] == 1000


# ---------------------------------------------------------------------------
# small files that are not a material concern
# ---------------------------------------------------------------------------


def test_small_files_not_material_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.small_files_not_material_table")
    assert len(fixture.snapshots) == 5
    assert len(fixture.data_files) == 5

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 5
    assert layout["total_record_count"] == 25
    assert layout["total_size_bytes"] < 20_000


# ---------------------------------------------------------------------------
# wide schema
# ---------------------------------------------------------------------------


def test_wide_schema_table(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.wide_schema_table")
    assert len(fixture.schema_fields) == 51
    assert len(fixture.data_files) == 1
    assert fixture.data_files[0].record_count == 1000
