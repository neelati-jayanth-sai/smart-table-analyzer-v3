"""Docker-local multi-symptom benchmark scenarios.

Architecture.md §41 and Runtime_Environments_UI.md §15, §52.

These tests seed real Iceberg tables through ``scripts.seed_local`` and assert
the deterministic tool outputs that the Investigator must interpret. They do not
invoke the Investigator model; the live-judgment scenarios are a separate
step after the deterministic coverage is verified.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sta.config import get_settings
from sta.context.context_builder import build_startup_context
from sta.context.schema_map import is_identifier_like
from sta.execution.backends.duckdb_candidate import DuckDbCandidateProfileProvider
from sta.execution.backends.local import LocalIcebergBackend
from sta.execution.backends.local_catalog import LocalCatalogProvider
from sta.execution.backends.pyiceberg_adapter import (
    table_fixture_from_pyiceberg,
    table_metadata_from_pyiceberg,
)
from sta.execution.errors import ParameterValidationError
from sta.execution.runner import QueryRunner
from sta.results.models import RunRecord
from sta.results.store import ResultStore
from sta.tools.partitions import PartitionCandidateParameters
from sta.tools.registry import DEFAULT_REGISTRY


def _fixture(seeded_catalog, name: str):
    py_table = seeded_catalog.load_table(name)
    return table_fixture_from_pyiceberg(py_table)


def _backend(seeded_catalog, name: str, *, with_provider: bool = False):
    fixture = _fixture(seeded_catalog, name)
    provider = None
    if with_provider:
        provider = DuckDbCandidateProfileProvider(
            fixture, s3_properties=get_settings().s3_properties()
        )
    return LocalIcebergBackend(fixture, candidate_profile_provider=provider), fixture


def _runner(backend: LocalIcebergBackend):
    store = ResultStore(":memory:")
    run = store.create_run(
        RunRecord(
            run_id="run_benchmark",
            table=backend.table,
            started_at="2024-01-01T00:00:00",
            status="running",
        )
    )
    return QueryRunner(
        backend=backend,
        store=store,
        run_id=run.run_id,
        table=backend.table,
        pinned_snapshot_id=str(backend.fixture.snapshot_id),
    )


# ---------------------------------------------------------------------------
# 1. large order/event table: poor partition spec, tiny files, many snapshots
# ---------------------------------------------------------------------------


def test_orders_bad_spec_has_many_small_files_and_snapshots(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.orders_bad_spec")
    py_table = seeded_catalog.load_table("demo.orders_bad_spec")
    expected_hours = 14 * 24
    expected_rows = expected_hours * 300

    assert len(fixture.snapshots) == expected_hours
    assert len(fixture.data_files) == expected_hours
    assert fixture.default_spec_id is not None
    assert "hour" in fixture.partition_specs[fixture.default_spec_id].fields[0].transform.lower()
    assert all(f.sort_order_id is None for f in fixture.data_files)
    assert py_table.metadata.properties.get("write.target-file-size-bytes") == "134217728"

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == expected_hours
    assert layout["total_record_count"] == expected_rows
    # Each hourly file is far below the 256-512 MiB operational target.
    assert layout["median_file_size_bytes"] < 200_000

    partition_layout = backend.execute(
        "get_partition_layout", {}, str(fixture.snapshot_id)
    ).rows
    assert len(partition_layout) == expected_hours

    manifest = backend.execute("get_manifest_stats", {}, str(fixture.snapshot_id)).rows[0]
    assert manifest["manifest_count"] == expected_hours


# ---------------------------------------------------------------------------
# 2. reasonable temporal partition with healthier layout
# ---------------------------------------------------------------------------


def test_orders_day_partitioned_is_healthier(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.orders_day_partitioned")
    py_table = seeded_catalog.load_table("demo.orders_day_partitioned")
    expected_days = 30
    expected_rows = expected_days * 10_000

    assert len(fixture.snapshots) == expected_days
    assert len(fixture.data_files) == expected_days
    assert "day" in fixture.partition_specs[fixture.default_spec_id].fields[0].transform.lower()
    assert py_table.metadata.properties.get("write.target-file-size-bytes") == "268435456"

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == expected_days
    assert layout["total_record_count"] == expected_rows
    # Daily files are much larger than the hourly tiny files in orders_bad_spec
    # but still modest in absolute terms for a local benchmark.
    assert layout["median_file_size_bytes"] > 100_000

    partition_layout = backend.execute(
        "get_partition_layout", {}, str(fixture.snapshot_id)
    ).rows
    assert len(partition_layout) == expected_days

    # The table declares an intentional sort order; actual file usage is a
    # separate measurement (Iceberg files do not always carry sort_order_id).
    assert fixture.default_sort_order_id is not None
    assert any(
        o.order_id == fixture.default_sort_order_id and o.fields
        for o in fixture.sort_orders
    )


# ---------------------------------------------------------------------------
# 3. temporal candidate table: targeted non-identifier partition analysis
# ---------------------------------------------------------------------------


def test_events_partition_candidate_temporal_analysis(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.events_partition_candidate", with_provider=True)

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 10
    assert layout["total_record_count"] == 50_000

    partition_rows = backend.execute(
        "get_partition_layout", {}, str(fixture.snapshot_id)
    ).rows
    assert not any(r["partition"] for r in partition_rows)
    assert len(partition_rows) == 1

    runner = _runner(backend)
    outcome = runner.run("analyze_partition_candidate", {"column": "event_timestamp"})
    payload = outcome.payload
    assert payload.total_value_count == 50_000
    assert payload.null_count == 0
    assert payload.distinct_count is not None
    assert payload.distinct_count >= 45_000
    assert payload.min_value is not None
    assert payload.max_value is not None


# ---------------------------------------------------------------------------
# 4. identifier-heavy table: candidate analysis must reject identifiers
# ---------------------------------------------------------------------------


def test_customer_orders_identifier_heavy_layout(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.customer_orders_identifier_heavy")
    identifier_names = [f.name for f in fixture.schema_fields if is_identifier_like(f.name)]
    assert "customer_id" in identifier_names
    assert "account_uuid" in identifier_names
    assert "transaction_guid" in identifier_names
    assert len(identifier_names) >= 7

    assert fixture.default_spec_id is not None
    assert "identity" in fixture.partition_specs[fixture.default_spec_id].fields[0].transform.lower()

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 2_000
    assert layout["total_record_count"] == 2_000

    partition_layout = backend.execute(
        "get_partition_layout", {}, str(fixture.snapshot_id)
    ).rows
    assert len(partition_layout) == 2_000
    assert all(r["file_count"] == 1 for r in partition_layout)


def test_customer_orders_identifier_heavy_rejects_candidate_analysis(seeded_catalog):
    backend, fixture = _backend(seeded_catalog, "demo.customer_orders_identifier_heavy", with_provider=True)
    runner = _runner(backend)
    identifier_columns = [f.name for f in fixture.schema_fields if is_identifier_like(f.name)]
    assert identifier_columns

    for column in identifier_columns:
        # The parameter contract rejects identifier-like columns before any
        # DuckDB scan or backend call is attempted.
        with pytest.raises(ParameterValidationError) as exc_info:
            runner.run("analyze_partition_candidate", {"column": column})
        assert "identifier-like" in str(exc_info.value)

        # Direct model construction also rejects the column.
        with pytest.raises(ValidationError):
            PartitionCandidateParameters(column=column)


# ---------------------------------------------------------------------------
# 5. casing test: bad partition spec + deliberately inert UPPERCASE properties
# ---------------------------------------------------------------------------

CASES_TABLE = "demo.orders_bad_spec_caps_properties"
CASES_HOURS = 3 * 24


def test_orders_bad_spec_caps_properties_layout_is_factual(seeded_catalog):
    """The bad ``hours(event_timestamp)`` spec is real and actually used.

    Every hourly append produced one tiny file in its own snapshot, exactly
    like ``orders_bad_spec`` — measured facts, independent of the inert
    uppercase properties also stored on the table.
    """
    backend, fixture = _backend(seeded_catalog, CASES_TABLE)
    expected_rows = CASES_HOURS * 200

    assert fixture.default_spec_id is not None
    spec = fixture.partition_specs[fixture.default_spec_id]
    assert spec.fields[0].source_id == 3  # event_timestamp
    assert "hour" in spec.fields[0].transform.lower()
    assert len(fixture.snapshots) == CASES_HOURS
    assert len(fixture.data_files) == CASES_HOURS
    assert all(f.sort_order_id is None for f in fixture.data_files)

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == CASES_HOURS
    assert layout["total_record_count"] == expected_rows
    # Files are tiny: the inert UPPERCASE target configured nothing, and the
    # engine default (plus 200 rows per write) is what actually applied.
    assert layout["median_file_size_bytes"] < 200_000

    partition_layout = backend.execute(
        "get_partition_layout", {}, str(fixture.snapshot_id)
    ).rows
    assert len(partition_layout) == CASES_HOURS

    manifest = backend.execute("get_manifest_stats", {}, str(fixture.snapshot_id)).rows[0]
    assert manifest["manifest_count"] == CASES_HOURS


def test_orders_bad_spec_caps_properties_uppercase_keys_are_inert(seeded_catalog):
    """UPPERCASE property keys are preserved as inert custom metadata.

    Iceberg property keys are case-sensitive; engines honor only the lowercase
    writer keys. These assertions pin the truthful behavior end to end:

    1. the uppercase keys round-trip verbatim through Iceberg metadata and the
       STA ``TableMetadata`` seam (preserved, never dropped or lowercased),
    2. no lowercase twin exists, so the uppercase keys configured nothing and
       actual file sizing stayed at the engine default,
    3. the compact TableContext keeps the uppercase keys visible verbatim as
       curated case variants alongside the effective lowercase key, so the
       Investigator can notice the inert keys without either key masquerading
       as the other (raw casing preserved, never normalized or merged),
    4. the effective metrics mode comes from the lowercase
       ``write.metadata.metrics.default`` alone, and the measured files really
       do carry full counts/bounds despite ``WRITE.METADATA.METRICS.DEFAULT``
       claiming "none" — the uppercase key had no effect.
    """
    py_table = seeded_catalog.load_table(CASES_TABLE)

    provider = LocalCatalogProvider(seeded_catalog)
    metadata = provider.load_table_metadata(CASES_TABLE)
    # Canonical UI/API identity: catalog prefix preserved through metadata.
    assert metadata.table == "local.demo.orders_bad_spec_caps_properties"

    # 1. Verbatim preservation as custom metadata (case-sensitive, values intact).
    assert metadata.properties["WRITE.TARGET-FILE-SIZE-BYTES"] == "134217728"
    assert metadata.properties["WRITE.PARQUET.ROW-GROUP-SIZE-BYTES"] == "8388608"
    assert metadata.properties["WRITE.METADATA.METRICS.DEFAULT"] == "none"
    assert py_table.metadata.properties["WRITE.TARGET-FILE-SIZE-BYTES"] == "134217728"

    # No lowercase twin exists for any inert key: the uppercase keys
    # configured nothing, so actual file sizing stayed at the engine default.
    # (PyIceberg itself adds its lowercase compression default; that is not a
    # twin of an uppercase key.)
    assert "write.target-file-size-bytes" not in metadata.properties
    assert "write.parquet.row-group-size-bytes" not in metadata.properties
    assert metadata.properties["write.metadata.metrics.default"] == "full"

    # 2/3. TableContext: known case-variant keys stay visible with their raw
    # casing next to the effective lowercase key (no normalization, no
    # overwriting), so the model-facing context can distinguish inert
    # uppercase keys from the one effective writer property.
    startup = build_startup_context(metadata)
    relevant = startup.table_context.relevant_table_properties
    assert relevant == {
        "WRITE.METADATA.METRICS.DEFAULT": "none",
        "WRITE.PARQUET.ROW-GROUP-SIZE-BYTES": "8388608",
        "WRITE.TARGET-FILE-SIZE-BYTES": "134217728",
        "write.metadata.metrics.default": "full",
    }
    assert startup.full_schema.properties == relevant
    # Availability is derived from the effective lowercase key, never from the
    # inert uppercase "none".
    assert set(startup.table_context.metrics_availability.values()) == {"full"}
    assert startup.table_context.current_partition_spec == "hours(event_timestamp)"

    # 4. Actual tool behavior stays factual: full metrics really exist on all
    # files — the uppercase "none" had no effect on the written metadata.
    backend, fixture = _backend(seeded_catalog, CASES_TABLE)
    metrics = backend.execute(
        "get_column_metadata_metrics", {"column": "order_id"}, str(fixture.snapshot_id)
    ).rows[0]
    assert metrics["files_measured"] == CASES_HOURS
    assert metrics["files_with_value_counts"] == CASES_HOURS
    assert metrics["files_with_bounds"] == CASES_HOURS

    # The adapter's TableMetadata carries the uppercase keys verbatim too.
    assert (
        table_metadata_from_pyiceberg(py_table).properties["WRITE.TARGET-FILE-SIZE-BYTES"]
        == "134217728"
    )


# ---------------------------------------------------------------------------
# critical architecture contract tests (Architecture.md §41 invariants)
# ---------------------------------------------------------------------------


def test_only_partition_candidate_is_targeted_scan():
    """There is exactly one expensive, single-column data-scan tool."""
    targeted = [
        name for name, spec in DEFAULT_REGISTRY.items() if spec.cost_class == "targeted-scan"
    ]
    assert targeted == ["analyze_partition_candidate"]

    # Other tools may reference a column (e.g. metadata metrics), but only
    # analyze_partition_candidate performs a bounded data scan.
    scan_tools = [
        name
        for name, spec in DEFAULT_REGISTRY.items()
        if spec.cost_class == "targeted-scan"
    ]
    assert scan_tools == ["analyze_partition_candidate"]


def test_metadata_tools_aggregate_without_data_scan(seeded_catalog):
    """Layout, partition and manifest tools return Iceberg metadata facts
    without opening data files."""
    backend, fixture = _backend(seeded_catalog, "demo.orders_bad_spec")

    layout = backend.execute("get_file_layout", {}, str(fixture.snapshot_id)).rows[0]
    assert layout["file_count"] == 336
    assert layout["total_record_count"] == sum(
        f.record_count for f in fixture.data_files if f.record_count is not None
    )

    partition_layout = backend.execute(
        "get_partition_layout", {}, str(fixture.snapshot_id)
    ).rows
    assert len(partition_layout) == 336
    assert sum(r["total_record_count"] for r in partition_layout) == layout["total_record_count"]

    manifest = backend.execute("get_manifest_stats", {}, str(fixture.snapshot_id)).rows[0]
    assert manifest["manifest_count"] == 336
    assert manifest["total_entries"] is not None
    assert manifest["total_entries"] >= 336

    metrics = backend.execute(
        "get_column_metadata_metrics", {"column": "order_id"}, str(fixture.snapshot_id)
    ).rows[0]
    assert metrics["files_measured"] == 336
    assert metrics["files_with_value_counts"] == 336
    assert metrics["files_with_bounds"] == 336
