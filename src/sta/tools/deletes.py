"""Delete-file statistics tool (Architecture.md #13 ``get_delete_file_stats``).

Measures the delete files (position / equality) referenced by the pinned
snapshot: counts, bytes and delete records where metadata reports them.
Metadata only — delete files are counted, never applied or read.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from sta.tools.spec import ToolSpec, median, single_row, sum_optional

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


class DeleteFileStatsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DeleteFileStatsResult(BaseModel):
    delete_file_count: int
    position_delete_file_count: int
    equality_delete_file_count: int
    total_delete_file_size_bytes: int
    # Delete records are available when delete-file metadata carries record
    # counts; None when no delete file reports one (never invented as 0).
    total_delete_records: int | None = None
    min_delete_file_size_bytes: int | None = None
    median_delete_file_size_bytes: float | None = None
    max_delete_file_size_bytes: int | None = None


def delete_file_stats_from_rows(
    rows: list[dict], _params: DeleteFileStatsParameters
) -> DeleteFileStatsResult:
    return single_row(rows, DeleteFileStatsResult)


DELETE_FILE_STATS_SPEC = ToolSpec(
    name="get_delete_file_stats",
    query_version="delete_file_stats:v1",
    description=(
        "Measures delete files at the pinned snapshot: counts by delete type, "
        "bytes, delete records where available, and size distribution."
    ),
    parameters=DeleteFileStatsParameters,
    result=DeleteFileStatsResult,
    build_payload=delete_file_stats_from_rows,
)


def get_delete_file_stats(
    runner: "QueryRunner", parameters: DeleteFileStatsParameters | dict | None = None
):
    """Measure delete-file statistics. Returns the stored ToolOutcome (Rxxx + payload)."""
    return runner.run(DELETE_FILE_STATS_SPEC.name, parameters)


# Re-exported for backends that build the aggregate row from per-file metadata.
__all__ = [
    "DELETE_FILE_STATS_SPEC",
    "DeleteFileStatsParameters",
    "DeleteFileStatsResult",
    "get_delete_file_stats",
    "delete_file_stats_from_rows",
    "median",
    "sum_optional",
]