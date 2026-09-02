"""Sort-order usage tool (Architecture.md #13 ``get_sort_order_usage``).

Measures which sort order the live data files actually carry, separating
"the table configures a sort order" (startup TableContext) from "existing
files were written with that sort order" (this tool; Architecture.md #16,
invariant 20). Files written without a sort order are grouped under
``sort_order_id=None``.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from sta.tools.spec import ToolSpec

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


class SortOrderUsageParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SortOrderFileUsage(BaseModel):
    """Data files at the pinned snapshot grouped by their recorded sort order."""

    sort_order_id: int | None = None
    file_count: int
    total_size_bytes: int


class SortOrderUsageResult(BaseModel):
    files_with_sort_order_id: int
    files_without_sort_order_id: int
    usage: list[SortOrderFileUsage]


def sort_order_usage_from_rows(
    rows: list[dict], _params: SortOrderUsageParameters
) -> SortOrderUsageResult:
    """Contract ordering: the unsorted group (``None``) first, then ascending
    sort-order ids; computed here so both backends order identically."""
    entries = [SortOrderFileUsage.model_validate(dict(row)) for row in rows]
    entries.sort(key=lambda entry: (entry.sort_order_id is not None, entry.sort_order_id or 0))
    return SortOrderUsageResult(
        files_with_sort_order_id=sum(entry.file_count for entry in entries if entry.sort_order_id is not None),
        files_without_sort_order_id=sum(entry.file_count for entry in entries if entry.sort_order_id is None),
        usage=entries,
    )


SORT_ORDER_USAGE_SPEC = ToolSpec(
    name="get_sort_order_usage",
    query_version="sort_order_usage:v1",
    description=(
        "Measures sort_order_id usage across the live data files, so a "
        "configured sort order is distinguished from files actually written "
        "with one."
    ),
    parameters=SortOrderUsageParameters,
    result=SortOrderUsageResult,
    build_payload=sort_order_usage_from_rows,
    entry_model=SortOrderFileUsage,
    rows_field="usage",
)


def get_sort_order_usage(
    runner: "QueryRunner", parameters: SortOrderUsageParameters | dict | None = None
):
    """Measure sort-order usage. Returns the stored ToolOutcome (Rxxx + payload)."""
    return runner.run(SORT_ORDER_USAGE_SPEC.name, parameters)