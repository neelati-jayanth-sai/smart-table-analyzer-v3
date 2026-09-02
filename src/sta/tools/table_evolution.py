"""Snapshot history tools (Architecture.md #13 ``get_snapshot_history``,
``get_file_layout_history``).

Both tools measure Iceberg snapshot metadata only — never table data.
``get_file_layout_history`` reads cumulative totals recorded in snapshot
summaries (``total-data-files`` / ``total-size-bytes`` / ``total-records``);
a missing summary key stays ``None`` (no invented values, Architecture.md #16).
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from sta.tools.spec import ToolSpec

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


class SnapshotHistoryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=1, le=1000)
    order: str = Field(default="newest_first", pattern="^(newest_first|oldest_first)$")


class SnapshotSummary(BaseModel):
    """One snapshot in the table's history.

    ``snapshot_id`` is a string so it survives engine integer widths.
    Summary-derived counters are ``None`` when the snapshot summary does not
    carry the corresponding Iceberg summary key.
    """

    snapshot_id: str
    parent_snapshot_id: str | None = None
    timestamp_ms: int | None = None
    operation: str | None = None
    added_data_files: int | None = None
    removed_data_files: int | None = None
    added_records: int | None = None
    removed_records: int | None = None


class SnapshotHistoryResult(BaseModel):
    snapshots: list[SnapshotSummary]


class FileLayoutHistoryParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=100, ge=1, le=1000)
    order: str = Field(default="newest_first", pattern="^(newest_first|oldest_first)$")


class FileLayoutHistoryEntry(BaseModel):
    """Cumulative data-layout totals as recorded in one snapshot summary."""

    snapshot_id: str
    timestamp_ms: int | None = None
    operation: str | None = None
    total_data_files: int | None = None
    total_data_size_bytes: int | None = None
    total_records: int | None = None
    added_data_files: int | None = None
    removed_data_files: int | None = None


class FileLayoutHistoryResult(BaseModel):
    entries: list[FileLayoutHistoryEntry]


def _sorted_by_snapshot(
    rows: list[dict], params: SnapshotHistoryParameters | FileLayoutHistoryParameters
) -> list[dict]:
    """Contract ordering: by numeric snapshot id, newest or oldest first.
    Applied in the shared builder so both backends order identically."""
    def snapshot_key(row: dict) -> tuple[int, str]:
        try:
            numeric = int(row["snapshot_id"])
        except (KeyError, TypeError, ValueError):
            numeric = -1
        return (numeric, str(row.get("snapshot_id")))

    newest_first = params.order == "newest_first"
    ordered = sorted(rows, key=snapshot_key, reverse=newest_first)
    return ordered[: params.limit]


def snapshot_history_from_rows(rows: list[dict], params: SnapshotHistoryParameters) -> SnapshotHistoryResult:
    return SnapshotHistoryResult(
        snapshots=[SnapshotSummary.model_validate(dict(row)) for row in _sorted_by_snapshot(rows, params)]
    )


def file_layout_history_from_rows(
    rows: list[dict], params: FileLayoutHistoryParameters
) -> FileLayoutHistoryResult:
    return FileLayoutHistoryResult(
        entries=[FileLayoutHistoryEntry.model_validate(dict(row)) for row in _sorted_by_snapshot(rows, params)]
    )


SNAPSHOT_HISTORY_SPEC = ToolSpec(
    name="get_snapshot_history",
    query_version="snapshot_history:v1",
    description=(
        "Measures the table's snapshot history: snapshot ids, timestamps, "
        "operations and added/removed files/records where the snapshot "
        "summary records them."
    ),
    parameters=SnapshotHistoryParameters,
    result=SnapshotHistoryResult,
    build_payload=snapshot_history_from_rows,
    snapshot_scoped=False,
    entry_model=SnapshotSummary,
    rows_field="snapshots",
)

FILE_LAYOUT_HISTORY_SPEC = ToolSpec(
    name="get_file_layout_history",
    query_version="file_layout_history:v1",
    description=(
        "Measures how the file layout changed across snapshots using the "
        "cumulative totals recorded in each snapshot summary."
    ),
    parameters=FileLayoutHistoryParameters,
    result=FileLayoutHistoryResult,
    build_payload=file_layout_history_from_rows,
    snapshot_scoped=False,
    entry_model=FileLayoutHistoryEntry,
    rows_field="entries",
)


def get_snapshot_history(
    runner: "QueryRunner", parameters: SnapshotHistoryParameters | dict | None = None
):
    """Measure snapshot history. Returns the stored ToolOutcome (Rxxx + payload)."""
    return runner.run(SNAPSHOT_HISTORY_SPEC.name, parameters)


def get_file_layout_history(
    runner: "QueryRunner", parameters: FileLayoutHistoryParameters | dict | None = None
):
    return runner.run(FILE_LAYOUT_HISTORY_SPEC.name, parameters)