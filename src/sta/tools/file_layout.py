"""File-layout tool (Architecture.md #13 ``get_file_layout``).

Measures the data-file layout of the pinned snapshot: file count, sizes and
record counts. Aggregate over Iceberg data-file metadata; never a data scan.

Contract units and semantics shared by both backends:

- sizes and counts are raw values in bytes / records (``*_bytes``),
- percentiles use linear interpolation between closest ranks
  (``sta.tools.spec.percentile`` — the same definition Spark ``percentile``
  uses, so production SQL and local computation agree),
- record-count statistics aggregate only the files that report record counts
  (``None`` when no file reports them; a missing measurement never becomes 0),
- an empty selection (no data files) keeps file_count 0 / total bytes 0 and
  reports every distribution field as ``None``.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from sta.tools.spec import ToolSpec, single_row

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


class FileLayoutParameters(BaseModel):
    """No parameters: the tool always measures the pinned snapshot."""

    model_config = ConfigDict(extra="forbid")


class FileLayoutResult(BaseModel):
    file_count: int
    total_size_bytes: int
    total_record_count: int | None = None
    min_file_size_bytes: int | None = None
    max_file_size_bytes: int | None = None
    avg_file_size_bytes: float | None = None
    median_file_size_bytes: float | None = None
    p25_file_size_bytes: float | None = None
    p90_file_size_bytes: float | None = None
    p95_file_size_bytes: float | None = None
    min_record_count: int | None = None
    max_record_count: int | None = None
    median_record_count: float | None = None


def file_layout_from_rows(rows: list[dict], _params: FileLayoutParameters) -> FileLayoutResult:
    return single_row(rows, FileLayoutResult)


FILE_LAYOUT_SPEC = ToolSpec(
    name="get_file_layout",
    query_version="file_layout:v1",
    description=(
        "Measures the data-file layout at the pinned snapshot: file count, "
        "total/min/max/average size, median and p25/p90/p95 file size, and "
        "record-count distribution."
    ),
    parameters=FileLayoutParameters,
    result=FileLayoutResult,
    build_payload=file_layout_from_rows,
    cost_class="metadata-aggregation",
)


def get_file_layout(
    runner: "QueryRunner", parameters: FileLayoutParameters | dict | None = None
):
    """Measure the data-file layout. Returns the stored ToolOutcome (Rxxx + payload)."""
    return runner.run(FILE_LAYOUT_SPEC.name, parameters)