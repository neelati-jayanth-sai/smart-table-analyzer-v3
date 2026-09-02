"""Local Iceberg backend (Runtime_Environments_UI.md #8-#13).

Computes every tool from *normalized* Iceberg metadata/data fixtures, so the
whole tool layer is testable without live Docker. The fixtures model the
Iceberg facts a live environment would supply (snapshot history, the pinned
snapshot's manifest list and files, per-file column metrics, targeted column
profiles). PyIceberg and DuckDB are used only at the backend boundary:

- ``sta.execution.backends.pyiceberg_adapter`` converts a live PyIceberg table
  into a :class:`LocalTableFixture` (no unit test needs Docker),
- ``sta.execution.backends.duckdb_candidate`` produces the targeted column
  profile for ``analyze_partition_candidate`` from real data files via a fixed
  local template (``queries/local/analyze_partition_candidate.sql``).

The pure fixture computations here never import DuckDB or PyIceberg.
"""

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, Field

from sta.context.context_builder import render_transform
from sta.context.table_metadata import (
    PartitionSpec,
    SchemaField,
    SortOrder,
    TableMetadata,
    TableSchema,
)
from sta.execution.backends.base import BackendExecution
from sta.execution.errors import (
    BackendExecutionError,
    ParameterValidationError,
    SnapshotNotAvailableError,
    TableNotResolvedError,
)
from sta.tools.spec import median, percentile, sum_optional

# Iceberg snapshot-summary keys standardized in the Iceberg spec (all values
# are strings in real metadata; writers omit keys they do not report).
_SUMMARY_ADDED_DATA_FILES = "added-data-files"
_SUMMARY_DELETED_DATA_FILES = "deleted-data-files"
_SUMMARY_ADDED_RECORDS = "added-records"
_SUMMARY_DELETED_RECORDS = "deleted-records"
_SUMMARY_TOTAL_DATA_FILES = "total-data-files"
_SUMMARY_TOTAL_SIZE_BYTES = "total-files-size"
_SUMMARY_TOTAL_RECORDS = "total-records"


# ---------------------------------------------------------------------------
# Normalized fixtures — the local backend's input contract
# ---------------------------------------------------------------------------


class LocalSnapshot(BaseModel):
    """One row of the snapshot history (metadata only)."""

    snapshot_id: int
    parent_snapshot_id: int | None = None
    timestamp_ms: int | None = None
    operation: str | None = None
    summary: dict[str, str] = Field(default_factory=dict)


class LocalManifestEntry(BaseModel):
    """One entry of the pinned snapshot's manifest list (metadata only: entry
    counts live on the manifest list, so manifest files need no I/O)."""

    manifest_path: str
    manifest_length_bytes: int
    content: int = 0  # 0 = data manifests, 1 = delete manifests (Iceberg spec)
    partition_spec_id: int = 0
    added_files_count: int | None = None
    existing_files_count: int | None = None
    deleted_files_count: int | None = None


class LocalFileEntry(BaseModel):
    """One file (data or delete) referenced by the pinned snapshot.

    ``content`` follows the Iceberg spec: 0 = data, 1 = position deletes,
    2 = equality deletes. ``partition`` maps the file's own partition spec
    field names to string-rendered values. ``sort_order_id`` is None for
    files written without a sort order."""

    file_path: str
    file_size_bytes: int
    content: int = 0
    file_format: str = "parquet"
    record_count: int | None = None
    partition: dict[str, str] = Field(default_factory=dict)
    partition_spec_id: int = 0
    sort_order_id: int | None = None


class LocalColumnMetrics(BaseModel):
    """Per-file Iceberg column metrics for one column, already decoded into
    typed values by the fixture provider: numeric bounds compare numerically,
    string bounds lexicographically; an uncomparable mix renders as None."""

    file_path: str
    value_count: int | None = None
    null_count: int | None = None
    nan_count: int | None = None
    lower_bound: int | float | str | None = None
    upper_bound: int | float | str | None = None


class LocalColumnProfile(BaseModel):
    """Targeted column profile produced by an engine (DuckDB locally, Spark in
    production) — the normalized data fixture for the expensive tool."""

    total_value_count: int | None = None
    null_count: int | None = None
    nan_count: int | None = None
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    # Optional deterministic distribution facts populated when the engine can
    # compute them. They are never scored or summarized into a recommendation.
    files_per_distinct_value_min: int | None = None
    files_per_distinct_value_median: float | None = None
    files_per_distinct_value_max: int | None = None
    records_per_distinct_value_min: int | None = None
    records_per_distinct_value_median: float | None = None
    records_per_distinct_value_max: int | None = None
    bytes_per_distinct_value_min: int | None = None
    bytes_per_distinct_value_median: float | None = None
    bytes_per_distinct_value_max: int | None = None
    top_values: list[dict[str, Any]] = Field(default_factory=list)
    values_truncated: bool = False


class LocalTableFixture(BaseModel):
    """Normalized Iceberg state of one table as of ``snapshot_id``.

    ``snapshot_id`` is ``None`` when the table has no snapshots (empty
    table): the absence is preserved, never fabricated as snapshot 0."""

    table: str
    snapshot_id: int | None = None
    schema_fields: list[SchemaField] = Field(default_factory=list)
    snapshots: list[LocalSnapshot] = Field(default_factory=list)
    manifests: list[LocalManifestEntry] = Field(default_factory=list)
    data_files: list[LocalFileEntry] = Field(default_factory=list)
    delete_files: list[LocalFileEntry] = Field(default_factory=list)
    column_metrics: dict[str, list[LocalColumnMetrics]] = Field(default_factory=dict)
    column_profiles: dict[str, LocalColumnProfile] = Field(default_factory=dict)
    partition_specs: list[PartitionSpec] = Field(default_factory=list)
    sort_orders: list[SortOrder] = Field(default_factory=list)
    default_spec_id: int | None = None
    default_sort_order_id: int | None = None


CandidateProfileProvider = Callable[[str], LocalColumnProfile | None]


# ---------------------------------------------------------------------------
# Pure fixture computations — one per tool, shared by the local backend
# ---------------------------------------------------------------------------


def _summary_int(snapshot: LocalSnapshot, key: str) -> int | None:
    raw = snapshot.summary.get(key)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _str_or_none(value: int | None) -> str | None:
    return None if value is None else str(value)


def snapshot_history_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    return [
        {
            "snapshot_id": str(snapshot.snapshot_id),
            "parent_snapshot_id": _str_or_none(snapshot.parent_snapshot_id),
            "timestamp_ms": snapshot.timestamp_ms,
            "operation": snapshot.operation,
            "added_data_files": _summary_int(snapshot, _SUMMARY_ADDED_DATA_FILES),
            "removed_data_files": _summary_int(snapshot, _SUMMARY_DELETED_DATA_FILES),
            "added_records": _summary_int(snapshot, _SUMMARY_ADDED_RECORDS),
            "removed_records": _summary_int(snapshot, _SUMMARY_DELETED_RECORDS),
        }
        for snapshot in fixture.snapshots
    ]


def file_layout_history_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    return [
        {
            "snapshot_id": str(snapshot.snapshot_id),
            "timestamp_ms": snapshot.timestamp_ms,
            "operation": snapshot.operation,
            "total_data_files": _summary_int(snapshot, _SUMMARY_TOTAL_DATA_FILES),
            "total_data_size_bytes": _summary_int(snapshot, _SUMMARY_TOTAL_SIZE_BYTES),
            "total_records": _summary_int(snapshot, _SUMMARY_TOTAL_RECORDS),
            "added_data_files": _summary_int(snapshot, _SUMMARY_ADDED_DATA_FILES),
            "removed_data_files": _summary_int(snapshot, _SUMMARY_DELETED_DATA_FILES),
        }
        for snapshot in fixture.snapshots
    ]


def file_layout_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    sizes = sorted(entry.file_size_bytes for entry in fixture.data_files)
    records = sorted(
        entry.record_count for entry in fixture.data_files if entry.record_count is not None
    )
    if not sizes:
        return [
            {
                "file_count": 0,
                "total_size_bytes": 0,
                "total_record_count": None,
                "min_file_size_bytes": None,
                "max_file_size_bytes": None,
                "avg_file_size_bytes": None,
                "median_file_size_bytes": None,
                "p25_file_size_bytes": None,
                "p90_file_size_bytes": None,
                "p95_file_size_bytes": None,
                "min_record_count": None,
                "max_record_count": None,
                "median_record_count": None,
            }
        ]
    return [
        {
            "file_count": len(sizes),
            "total_size_bytes": sum(sizes),
            "total_record_count": sum_optional(
                [entry.record_count for entry in fixture.data_files]
            ),
            "min_file_size_bytes": sizes[0],
            "max_file_size_bytes": sizes[-1],
            "avg_file_size_bytes": sum(sizes) / len(sizes),
            "median_file_size_bytes": percentile(sizes, 0.5),
            "p25_file_size_bytes": percentile(sizes, 0.25),
            "p90_file_size_bytes": percentile(sizes, 0.90),
            "p95_file_size_bytes": percentile(sizes, 0.95),
            "min_record_count": records[0] if records else None,
            "max_record_count": records[-1] if records else None,
            "median_record_count": median(records),
        }
    ]


def partition_layout_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, tuple[tuple[str, str], ...]], list[LocalFileEntry]] = {}
    for entry in fixture.data_files:
        key = (entry.partition_spec_id, tuple(sorted(entry.partition.items())))
        grouped.setdefault(key, []).append(entry)
    return [
        {
            "partition": dict(partition_items),
            "spec_id": spec_id,
            "file_count": len(entries),
            "total_size_bytes": sum(entry.file_size_bytes for entry in entries),
            "total_record_count": sum_optional([entry.record_count for entry in entries]),
        }
        for (spec_id, partition_items), entries in grouped.items()
    ]


def _entry_count(value: int | None) -> int:
    return 0 if value is None else value


def _sum_manifest_entries(
    manifests: list[LocalManifestEntry], contents: frozenset[int], include_deleted: bool
) -> int | None:
    """Sum manifest-list entry counts; 0 for an empty selection (provably no
    entries), None when manifests exist but report no counts at all."""
    selected = [entry for entry in manifests if entry.content in contents]
    if not selected:
        return 0
    per_manifest: list[int | None] = []
    for entry in selected:
        parts = [entry.added_files_count, entry.existing_files_count]
        if include_deleted:
            parts.append(entry.deleted_files_count)
        if any(part is not None for part in parts):
            per_manifest.append(sum(_entry_count(part) for part in parts))
        else:
            per_manifest.append(None)
    return sum_optional(per_manifest)


def manifest_stats_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    manifests = fixture.manifests
    lengths = [entry.manifest_length_bytes for entry in manifests]
    total_entries = _sum_manifest_entries(manifests, frozenset({0, 1}), include_deleted=True)
    return [
        {
            "manifest_count": len(manifests),
            "total_manifest_size_bytes": sum(lengths),
            "total_entries": total_entries,
            "live_data_file_entries": _sum_manifest_entries(
                manifests, frozenset({0}), include_deleted=False
            ),
            "live_delete_file_entries": _sum_manifest_entries(
                manifests, frozenset({1}), include_deleted=False
            ),
            "deleted_entries": _deleted_entries(manifests),
            "avg_manifest_size_bytes": (sum(lengths) / len(lengths)) if lengths else None,
            "avg_entries_per_manifest": (total_entries / len(manifests))
            if total_entries is not None and manifests
            else None,
            "min_manifest_size_bytes": min(lengths) if lengths else None,
            "max_manifest_size_bytes": max(lengths) if lengths else None,
        }
    ]


def _deleted_entries(manifests: list[LocalManifestEntry]) -> int | None:
    selected = [entry for entry in manifests if entry.deleted_files_count is not None]
    if not manifests:
        return 0
    if not selected:
        return None
    return sum(_entry_count(entry.deleted_files_count) for entry in selected)


def delete_file_stats_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    deletes = fixture.delete_files
    sizes = sorted(entry.file_size_bytes for entry in deletes)
    return [
        {
            "delete_file_count": len(deletes),
            "position_delete_file_count": sum(1 for entry in deletes if entry.content == 1),
            "equality_delete_file_count": sum(1 for entry in deletes if entry.content == 2),
            "total_delete_file_size_bytes": sum(sizes),
            "total_delete_records": sum_optional([entry.record_count for entry in deletes]),
            "min_delete_file_size_bytes": sizes[0] if sizes else None,
            "median_delete_file_size_bytes": median(sizes),
            "max_delete_file_size_bytes": sizes[-1] if sizes else None,
        }
    ]


def column_metrics_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    column = params["column"]
    entries = fixture.column_metrics.get(column, [])
    return [
        {
            "column": column,
            "field_id": _field_id(fixture, column),
            "files_measured": len(fixture.data_files),
            "files_with_value_counts": sum(
                1 for entry in entries if entry.value_count is not None
            ),
            "files_with_bounds": sum(
                1 for entry in entries if entry.lower_bound is not None
            ),
            "value_count_sum": sum_optional([entry.value_count for entry in entries]),
            "null_value_count_sum": sum_optional([entry.null_count for entry in entries]),
            "nan_value_count_sum": sum_optional([entry.nan_count for entry in entries]),
            "lower_bound": _render_bound(
                _extreme(entries, "lower_bound", min)
            ),
            "upper_bound": _render_bound(
                _extreme(entries, "upper_bound", max)
            ),
        }
    ]


def _extreme(
    entries: list[LocalColumnMetrics], attribute: str, extreme
) -> int | float | str | None:
    """Smallest/largest observed bound value. Numeric bounds compare
    numerically, string bounds lexicographically; a type mix renders None."""
    values = [getattr(entry, attribute) for entry in entries]
    values = [value for value in values if value is not None]
    if not values:
        return None
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in values):
        return extreme(values)
    if all(isinstance(value, str) for value in values):
        return extreme(values)
    return None


def _render_bound(value: int | float | str | None) -> str | None:
    return None if value is None else str(value)


def partition_candidate_rows(
    fixture: LocalTableFixture,
    params,
    provider: CandidateProfileProvider | None = None,
) -> list[dict[str, Any]]:
    column = params["column"]
    profile = fixture.column_profiles.get(column)
    if profile is None and provider is not None:
        profile = provider(column)
    if profile is None:
        raise BackendExecutionError(
            "analyze_partition_candidate",
            f"no targeted column profile is available for column {column!r} "
            "on the local backend",
        )
    row = profile.model_dump()
    row["column"] = column
    row["field_id"] = _field_id(fixture, column)
    return [row]


def partition_spec_usage_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    fields_by_id = {schema_field.field_id: schema_field.name for schema_field in fixture.schema_fields}
    usage: dict[int, tuple[int, int]] = {}
    for entry in fixture.data_files:
        file_count, size = usage.get(entry.partition_spec_id, (0, 0))
        usage[entry.partition_spec_id] = (file_count + 1, size + entry.file_size_bytes)

    spec_ids = {spec.spec_id for spec in fixture.partition_specs}
    rows = [
        {
            "spec_id": spec.spec_id,
            "fields": [
                render_transform(field.transform, fields_by_id.get(field.source_id, field.name))
                for field in spec.fields
            ],
            "file_count": usage.get(spec.spec_id, (0, 0))[0],
            "total_size_bytes": usage.get(spec.spec_id, (0, 0))[1],
        }
        for spec in fixture.partition_specs
    ]
    # A live file referencing a spec the fixture does not declare is surfaced
    # with empty fields rather than silently dropped.
    for spec_id in sorted(set(usage) - spec_ids):
        file_count, size = usage[spec_id]
        rows.append(
            {"spec_id": spec_id, "fields": [], "file_count": file_count, "total_size_bytes": size}
        )
    return rows


def sort_order_usage_rows(fixture: LocalTableFixture, params) -> list[dict[str, Any]]:
    grouped: dict[int | None, list[LocalFileEntry]] = {}
    for entry in fixture.data_files:
        grouped.setdefault(entry.sort_order_id, []).append(entry)
    return [
        {
            "sort_order_id": sort_order_id,
            "file_count": len(entries),
            "total_size_bytes": sum(entry.file_size_bytes for entry in entries),
        }
        for sort_order_id, entries in sorted(
            grouped.items(), key=lambda item: (item[0] is not None, item[0] or 0)
        )
    ]


def _field_id(fixture: LocalTableFixture, column: str) -> int | None:
    for schema_field in fixture.schema_fields:
        if schema_field.name == column:
            return schema_field.field_id
    return None


_TOOL_FUNCTIONS = {
    "get_snapshot_history": snapshot_history_rows,
    "get_file_layout": file_layout_rows,
    "get_file_layout_history": file_layout_history_rows,
    "get_partition_layout": partition_layout_rows,
    "get_manifest_stats": manifest_stats_rows,
    "get_delete_file_stats": delete_file_stats_rows,
    "get_column_metadata_metrics": column_metrics_rows,
    "analyze_partition_candidate": partition_candidate_rows,
    "get_partition_spec_usage": partition_spec_usage_rows,
    "get_sort_order_usage": sort_order_usage_rows,
}


class LocalIcebergBackend:
    """``TableBackend`` for the local Docker Iceberg environment.

    Built from a normalized :class:`LocalTableFixture` (tests) or from the
    PyIceberg adapter for live tables. DuckDB/PyIceberg are only ever touched
    by the ``candidate_profile_provider`` seam passed in here — never by the
    tool code itself.
    """

    name = "local"

    def __init__(
        self,
        fixture: LocalTableFixture,
        candidate_profile_provider: CandidateProfileProvider | None = None,
    ) -> None:
        self._fixture = fixture
        self._candidate_profile_provider = candidate_profile_provider

    @property
    def table(self) -> str:
        return self._fixture.table

    @property
    def fixture(self) -> LocalTableFixture:
        return self._fixture

    def supported_tools(self) -> frozenset[str]:
        # get_iomete_maintenance_config is IOMETE-only (Runtime doc #5, #13).
        return frozenset(_TOOL_FUNCTIONS)

    def load_table_metadata(self, table_name: str) -> TableMetadata:
        """Resolve a table name into backend-independent metadata
        (``TableMetadataProvider`` seam). Raises for unknown names."""
        if table_name != self._fixture.table:
            raise TableNotResolvedError(table_name)
        fixture = self._fixture
        return TableMetadata(
            table=fixture.table,
            schemas=[TableSchema(schema_id=0, fields=list(fixture.schema_fields))],
            current_schema_id=0,
            current_snapshot_id=fixture.snapshot_id,
            partition_specs=fixture.partition_specs or None,
            default_spec_id=fixture.default_spec_id,
            sort_orders=fixture.sort_orders or None,
            default_sort_order_id=fixture.default_sort_order_id,
        )

    def execute(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        snapshot_id: str | None,
    ) -> BackendExecution:
        function = _TOOL_FUNCTIONS.get(tool_name)
        if function is None:
            raise BackendExecutionError(tool_name, f"tool {tool_name!r} has no local implementation")
        self._require_snapshot(tool_name, snapshot_id)
        try:
            if tool_name == "analyze_partition_candidate":
                rows = partition_candidate_rows(
                    self._fixture, parameters, self._candidate_profile_provider
                )
            else:
                rows = function(self._fixture, parameters)
        except ParameterValidationError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise BackendExecutionError(tool_name, "local computation failed") from exc
        # The scope is what the fixture actually represents: None when the
        # table has no snapshots. Fabricating a snapshot id here would
        # misattribute evidence (Architecture.md #15); the runner turns a
        # missing scope on a snapshot-scoped tool into a typed rejection.
        return BackendExecution(rows=rows, snapshot_id=_snapshot_scope(self._fixture.snapshot_id))

    def _require_snapshot(self, tool_name: str, snapshot_id: str | None) -> None:
        fixture_snapshot = self._fixture.snapshot_id
        if snapshot_id is None:
            return
        if fixture_snapshot is not None and str(fixture_snapshot) == snapshot_id:
            return
        measured = "no snapshot (empty table)" if fixture_snapshot is None else str(fixture_snapshot)
        raise SnapshotNotAvailableError(
            tool_name,
            f"local fixture represents {measured}, requested snapshot {snapshot_id}",
        )


def _snapshot_scope(snapshot_id: int | None) -> str | None:
    return None if snapshot_id is None else str(snapshot_id)