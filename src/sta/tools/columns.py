"""Column metadata-metrics tool (Architecture.md #13/#14
``get_column_metadata_metrics``).

Reads per-file Iceberg column metrics (value counts, null counts, NaN counts,
lower/upper bounds) from metadata only. No table scan: where the table's
metrics configuration does not record a measurement, the field is ``None``
(missing metrics do not mean missing data, Architecture.md #16).
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from sta.tools.spec import ToolSpec, single_row, sum_optional

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


class ColumnMetadataMetricsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1)


class ColumnMetadataMetricsResult(BaseModel):
    column: str
    field_id: int | None = None
    files_measured: int
    files_with_value_counts: int
    files_with_bounds: int
    value_count_sum: int | None = None
    null_value_count_sum: int | None = None
    nan_value_count_sum: int | None = None
    # Bounds are deterministic string renderings of the smallest observed
    # lower bound / largest observed upper bound (rendering is normalized by
    # each backend adapter; None when no file reports bounds).
    lower_bound: str | None = None
    upper_bound: str | None = None


def column_metrics_from_rows(
    rows: list[dict], _params: ColumnMetadataMetricsParameters
) -> ColumnMetadataMetricsResult:
    return single_row(rows, ColumnMetadataMetricsResult)


COLUMN_METADATA_METRICS_SPEC = ToolSpec(
    name="get_column_metadata_metrics",
    query_version="column_metadata_metrics:v1",
    description=(
        "Metadata-first column evidence: value/null/NaN counts and lower/upper "
        "bounds aggregated from Iceberg per-file metrics for one column. "
        "No table scan."
    ),
    parameters=ColumnMetadataMetricsParameters,
    result=ColumnMetadataMetricsResult,
    build_payload=column_metrics_from_rows,
)


def get_column_metadata_metrics(
    runner: "QueryRunner", parameters: ColumnMetadataMetricsParameters | dict
):
    """Measure one column's file-level metadata metrics (metadata only).

    Returns the stored ToolOutcome (Rxxx + payload).
    """
    return runner.run(COLUMN_METADATA_METRICS_SPEC.name, parameters)


__all__ = [
    "COLUMN_METADATA_METRICS_SPEC",
    "ColumnMetadataMetricsParameters",
    "ColumnMetadataMetricsResult",
    "column_metrics_from_rows",
    "get_column_metadata_metrics",
    "sum_optional",
]