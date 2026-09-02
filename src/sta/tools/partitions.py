"""Partition measurement tools (Architecture.md #13):
``get_partition_layout``, ``analyze_partition_candidate`` and
``get_partition_spec_usage``.

All three measure only; interpretation and recommendations belong to the
Investigator. ``analyze_partition_candidate`` is the single expensive
targeted tool and hard-rejects identifier-like columns (Architecture.md #10,
#39; STA invariant 14) in its validated parameter contract, so the rejection
holds for every backend before any query runs.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, field_validator

from sta.context.schema_map import is_identifier_like
from sta.tools.spec import ToolSpec, median, single_row

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


# ---------------------------------------------------------------------------
# get_partition_layout
# ---------------------------------------------------------------------------


class PartitionLayoutParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=25, ge=1, le=500)


class PartitionLayoutEntry(BaseModel):
    """One physical partition. ``partition`` maps the partition field names of
    the file's own spec to their (string-rendered) values, so entries stay
    self-describing across partition-spec evolution."""

    partition: dict[str, str]
    spec_id: int
    file_count: int
    total_size_bytes: int
    total_record_count: int | None = None


class PartitionLayoutResult(BaseModel):
    partitioned: bool
    partition_count: int
    entries: list[PartitionLayoutEntry]
    largest_partition: PartitionLayoutEntry | None = None
    smallest_partition: PartitionLayoutEntry | None = None
    files_per_partition_min: int | None = None
    files_per_partition_median: float | None = None
    files_per_partition_max: int | None = None
    size_bytes_per_partition_min: int | None = None
    size_bytes_per_partition_median: float | None = None
    size_bytes_per_partition_max: int | None = None


def partition_layout_from_rows(
    rows: list[dict], params: PartitionLayoutParameters
) -> PartitionLayoutResult:
    """Shape raw per-partition aggregate rows into the bounded contract.

    Backends must emit one row per physical partition with no LIMIT in the
    query template, because the shared distribution statistics (min, max,
    median files/bytes per partition and largest/smallest partition) are
    computed over the full population. Only the returned ``entries`` list is
    bounded by ``limit``. An unpartitioned table is reported as exactly one
    logical partition with an empty ``partition`` dict and ``partitioned=False``.
    """
    if not rows:
        return PartitionLayoutResult(partitioned=False, partition_count=0, entries=[])

    entries = [
        PartitionLayoutEntry.model_validate(dict(row))
        for row in sorted(
            rows,
            key=lambda row: (
                -int(row["total_size_bytes"]),
                int(row["spec_id"]),
                str(sorted(row["partition"].items())),
            ),
        )
    ]
    file_counts = [entry.file_count for entry in entries]
    sizes = [entry.total_size_bytes for entry in entries]
    return PartitionLayoutResult(
        partitioned=any(entry.partition for entry in entries),
        partition_count=len(entries),
        entries=entries[: params.limit],
        largest_partition=max(entries, key=lambda entry: (entry.total_size_bytes, _partition_key(entry))),
        smallest_partition=min(entries, key=lambda entry: (entry.total_size_bytes, _partition_key(entry))),
        files_per_partition_min=min(file_counts),
        files_per_partition_median=median(sorted(file_counts)),
        files_per_partition_max=max(file_counts),
        size_bytes_per_partition_min=min(sizes),
        size_bytes_per_partition_median=median(sorted(sizes)),
        size_bytes_per_partition_max=max(sizes),
    )


def _partition_key(entry: PartitionLayoutEntry) -> str:
    return str(sorted(entry.partition.items()))


PARTITION_LAYOUT_SPEC = ToolSpec(
    name="get_partition_layout",
    query_version="partition_layout:v1",
    description=(
        "Measures the physical partition layout at the pinned snapshot: "
        "partition count, files/bytes/records per partition, distribution "
        "statistics and the largest/smallest partitions."
    ),
    parameters=PartitionLayoutParameters,
    result=PartitionLayoutResult,
    build_payload=partition_layout_from_rows,
    cost_class="metadata-aggregation",
    entry_model=PartitionLayoutEntry,
    rows_field="entries",
)


def get_partition_layout(
    runner: "QueryRunner", parameters: PartitionLayoutParameters | dict | None = None
):
    return runner.run(PARTITION_LAYOUT_SPEC.name, parameters)


# ---------------------------------------------------------------------------
# analyze_partition_candidate — the single expensive targeted tool
# ---------------------------------------------------------------------------


class PartitionCandidateParameters(BaseModel):
    """One selected column, measured with a predefined aggregate query.

    Identifier-like columns are rejected here — before any backend is touched
    — so expensive analysis can never run on surrogate keys
    (Architecture.md #10/#39, invariant 14). The name-based policy comes from
    ``sta.context.schema_map``; backends additionally verify the column exists
    in the current schema.
    """

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1)

    @field_validator("column")
    @classmethod
    def reject_identifier_columns(cls, value: str) -> str:
        if is_identifier_like(value):
            raise ValueError(
                f"column {value!r} is identifier-like; expensive partition-candidate "
                "analysis is excluded for identifier-like columns"
            )
        return value


class CandidateValueBucket(BaseModel):
    """One distinct candidate value and the files/records that carry it.

    These are deterministic distribution facts: they describe how the
    existing data would group if the table were partitioned by this value.
    They never judge whether the value makes a good partition key."""

    model_config = ConfigDict(extra="forbid")

    value: str | None = None
    file_count: int
    record_count: int
    total_size_bytes: int | None = None


class PartitionCandidateResult(BaseModel):
    """Bounded column profile measurements plus distribution facts.

    Suitability is never judged here. Distribution facts (files/records per
    distinct value) let the Investigator compare the candidate against the
    current partition spec using only measurements."""

    column: str
    field_id: int | None = None
    total_value_count: int | None = None
    null_count: int | None = None
    nan_count: int | None = None
    distinct_count: int | None = None
    min_value: str | None = None
    max_value: str | None = None
    # Distribution facts for comparing the candidate to the current spec.
    files_per_distinct_value_min: int | None = None
    files_per_distinct_value_median: float | None = None
    files_per_distinct_value_max: int | None = None
    records_per_distinct_value_min: int | None = None
    records_per_distinct_value_median: float | None = None
    records_per_distinct_value_max: int | None = None
    bytes_per_distinct_value_min: int | None = None
    bytes_per_distinct_value_median: float | None = None
    bytes_per_distinct_value_max: int | None = None
    top_values: list[CandidateValueBucket] = Field(default_factory=list)
    values_truncated: bool = False


def partition_candidate_from_rows(
    rows: list[dict], params: PartitionCandidateParameters
) -> PartitionCandidateResult:
    return single_row(rows, PartitionCandidateResult)


PARTITION_CANDIDATE_SPEC = ToolSpec(
    name="analyze_partition_candidate",
    query_version="partition_candidate:v2",
    description=(
        "Targeted expensive tool: bounded aggregate profile of one selected "
        "column (nulls, NaNs, distinct count, min/max) plus deterministic "
        "distribution facts (files/records per distinct value) so the "
        "Investigator can compare the candidate against the current partition "
        "spec. Identifier-like columns are rejected. Run only when cheaper "
        "metadata is insufficient."
    ),
    parameters=PartitionCandidateParameters,
    result=PartitionCandidateResult,
    build_payload=partition_candidate_from_rows,
    cost_class="targeted-scan",
)


def analyze_partition_candidate(
    runner: "QueryRunner", parameters: PartitionCandidateParameters | dict
):
    return runner.run(PARTITION_CANDIDATE_SPEC.name, parameters)


# ---------------------------------------------------------------------------
# get_partition_spec_usage
# ---------------------------------------------------------------------------


class PartitionSpecUsageParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PartitionSpecUsageEntry(BaseModel):
    """Data files at the pinned snapshot grouped by the partition spec they
    were written under. ``fields`` are the spec's rendered transforms (e.g.
    ``days(created_at)``); ``total_size_bytes`` is ``None`` when the backend
    surface cannot provide per-spec sizes."""

    spec_id: int
    fields: list[str]
    file_count: int
    total_size_bytes: int | None = None


class PartitionSpecUsageResult(BaseModel):
    specs: list[PartitionSpecUsageEntry]


def partition_spec_usage_from_rows(
    rows: list[dict], _params: PartitionSpecUsageParameters
) -> PartitionSpecUsageResult:
    entries = [PartitionSpecUsageEntry.model_validate(dict(row)) for row in rows]
    entries.sort(key=lambda entry: entry.spec_id)
    return PartitionSpecUsageResult(specs=entries)


PARTITION_SPEC_USAGE_SPEC = ToolSpec(
    name="get_partition_spec_usage",
    query_version="partition_spec_usage:v1",
    description=(
        "Measures which partition specs the live data files were written "
        "under, current and historical, so spec evolution and existing-file "
        "rewrites are distinguished."
    ),
    parameters=PartitionSpecUsageParameters,
    result=PartitionSpecUsageResult,
    build_payload=partition_spec_usage_from_rows,
    entry_model=PartitionSpecUsageEntry,
    rows_field="specs",
)


def get_partition_spec_usage(
    runner: "QueryRunner", parameters: PartitionSpecUsageParameters | dict | None = None
):
    return runner.run(PARTITION_SPEC_USAGE_SPEC.name, parameters)