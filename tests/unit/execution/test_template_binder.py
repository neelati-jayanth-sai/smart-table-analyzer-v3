"""Reviewed query template loader/binder tests (Architecture.md #12).

Verifies that every IOMETE tool has a checked-in template, placeholders are
valid, and bindings reject identifier-injection attempts."""

import pytest

from sta.execution.queries.loader import (
    bind_template,
    iomete_template_tools,
    load_template,
    template_placeholders,
)
from sta.tools.registry import DEFAULT_REGISTRY


def test_all_iomete_registry_tools_have_templates() -> None:
    """Every tool in DEFAULT_REGISTRY that is not local-only must have an IOMETE template."""
    templates = iomete_template_tools()
    for name, spec in DEFAULT_REGISTRY.items():
        if name == "get_iomete_maintenance_config":
            # Template exists but is enabled only when maintenance_table is configured.
            assert name in templates, name
            continue
        assert name in templates, f"missing reviewed IOMETE template for {name}"


def test_local_candidate_template_exists_and_binds() -> None:
    template = load_template("local", "analyze_partition_candidate")
    assert ":source" in template
    assert ":column" in template
    bound = bind_template(
        "local",
        "analyze_partition_candidate",
        {"source": "read_parquet('s3://bucket/table')", "column": "created_at"},
    )
    assert "read_parquet('s3://bucket/table')" in bound
    assert "created_at" in bound


def test_iomete_templates_have_only_bindable_placeholders() -> None:
    for name in iomete_template_tools():
        template = load_template("iomete", name)
        placeholders = template_placeholders(template)
        allowed = {"table", "maintenance_table", "column", "snapshot_id", "limit", "field_id"}
        assert placeholders <= allowed, f"{name} has unexpected placeholders: {placeholders - allowed}"


def test_bind_template_rejects_bad_table_name() -> None:
    with pytest.raises(Exception) as exc_info:
        bind_template(
            "iomete",
            "get_file_layout",
            {"table": "bad; injection", "snapshot_id": "123"},
        )
    assert "identifier" in str(exc_info.value).lower()


def test_bind_template_rejects_bad_column_name() -> None:
    with pytest.raises(Exception) as exc_info:
        bind_template(
            "iomete",
            "get_column_metadata_metrics",
            {"table": "cat.db.t", "snapshot_id": "123", "column": "1bad", "field_id": "3"},
        )
    assert "identifier" in str(exc_info.value).lower()


def test_bind_template_rejects_non_integer_snapshot() -> None:
    with pytest.raises(Exception) as exc_info:
        bind_template(
            "iomete",
            "get_file_layout",
            {"table": "cat.db.t", "snapshot_id": "abc"},
        )
    assert "integer" in str(exc_info.value).lower()


def test_bind_template_rejects_missing_placeholder() -> None:
    with pytest.raises(Exception) as exc_info:
        bind_template("iomete", "get_file_layout", {"table": "cat.db.t"})
    assert "missing required bindings" in str(exc_info.value).lower()


def test_partition_layout_template_has_no_limit() -> None:
    """get_partition_layout must return the full population so distribution
    stats are computed over every partition; bounding belongs in the shared
    tool contract, not the IOMETE SQL."""
    template = load_template("iomete", "get_partition_layout")
    placeholders = template_placeholders(template)
    assert "limit" not in placeholders
    bound = bind_template(
        "iomete",
        "get_partition_layout",
        {"table": "cat.db.t", "snapshot_id": "123"},
    )
    for sql in (template, bound):
        # Strip comments, then verify there is no LIMIT clause after ORDER BY.
        body = "\n".join(
            line for line in sql.splitlines() if not line.strip().startswith("--")
        )
        assert "LIMIT" not in body.upper(), "template must not LIMIT partition rows"



def test_snapshot_history_template_casts_summary_strings_to_int() -> None:
    """Raw Iceberg summary map values are strings; the template must cast them
    so the shared contract receives int | None instead of strings."""
    bound = bind_template(
        "iomete",
        "get_snapshot_history",
        {"table": "cat.db.t", "limit": "50"},
    )
    for key in (
        "added-data-files",
        "deleted-data-files",
        "added-records",
        "deleted-records",
    ):
        assert f"cast(summary['{key}'] AS BIGINT)" in bound, key


def test_file_layout_history_template_casts_summary_strings_to_int() -> None:
    """Raw Iceberg summary map values are strings; the file-layout history
    template must cast cumulative totals to BIGINT to match the local
    contract (int | None)."""
    bound = bind_template(
        "iomete",
        "get_file_layout_history",
        {"table": "cat.db.t", "limit": "50"},
    )
    for key in (
        "total-data-files",
        "total-files-size",
        "total-records",
        "added-data-files",
        "deleted-data-files",
    ):
        assert f"cast(summary['{key}'] AS BIGINT)" in bound, key


def test_manifest_stats_template_is_null_safe_and_content_filtered() -> None:
    """Manifest entry-count aggregations must treat NULL as 0 and keep data
    manifests (content=0) separate from delete manifests (content=1)."""
    bound = bind_template(
        "iomete",
        "get_manifest_stats",
        {"table": "cat.db.t", "snapshot_id": "123"},
    ).upper()
    # Raw NULL-unsafe sums must not remain.
    assert "SUM(ADDED_FILES_COUNT + EXISTING_FILES_COUNT" not in bound
    assert "SUM(ADDED_FILES_COUNT + EXISTING_FILES_COUNT + DELETED_FILES_COUNT)" not in bound
    # NULL-safe coalesce used inside aggregates.
    assert "COALESCE(ADDED_FILES_COUNT, 0)" in bound
    assert "COALESCE(EXISTING_FILES_COUNT, 0)" in bound
    assert "COALESCE(DELETED_FILES_COUNT, 0)" in bound
    # Data/delete manifests are counted separately.
    assert "CONTENT = 0" in bound
    assert "CONTENT = 1" in bound


def test_delete_file_stats_template_null_safe_total_delete_records() -> None:
    """total_delete_records must stay NULL when no delete file reports a
    record count, matching the local sum_optional contract."""
    bound = bind_template(
        "iomete",
        "get_delete_file_stats",
        {"table": "cat.db.t", "snapshot_id": "123"},
    ).upper()
    assert "CASE WHEN COUNT(RECORD_COUNT) > 0 THEN SUM(RECORD_COUNT) END" in bound

