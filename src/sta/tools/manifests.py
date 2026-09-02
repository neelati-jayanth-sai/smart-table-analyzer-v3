"""Manifest statistics tool (Architecture.md #13 ``get_manifest_stats``).

Measures the manifest list of the pinned snapshot: manifest count, sizes and
entry counts (added/existing/deleted). A large share of deleted entries
relative to live entries is observable as data here — the tool itself does
not interpret it. Iceberg manifest-list metadata carries the entry counts, so
no manifest files need to be read.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from sta.tools.spec import ToolSpec, single_row

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


class ManifestStatsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ManifestStatsResult(BaseModel):
    manifest_count: int
    total_manifest_size_bytes: int
    # Entry counts come from manifest-list metadata; None when the surface
    # does not report the entry counts at all.
    total_entries: int | None = None
    live_data_file_entries: int | None = None
    live_delete_file_entries: int | None = None
    deleted_entries: int | None = None
    avg_manifest_size_bytes: float | None = None
    avg_entries_per_manifest: float | None = None
    min_manifest_size_bytes: int | None = None
    max_manifest_size_bytes: int | None = None


def manifest_stats_from_rows(rows: list[dict], _params: ManifestStatsParameters) -> ManifestStatsResult:
    return single_row(rows, ManifestStatsResult)


MANIFEST_STATS_SPEC = ToolSpec(
    name="get_manifest_stats",
    query_version="manifest_stats:v1",
    description=(
        "Measures the pinned snapshot's manifests: manifest count, sizes, "
        "live and deleted entry counts, and per-manifest averages."
    ),
    parameters=ManifestStatsParameters,
    result=ManifestStatsResult,
    build_payload=manifest_stats_from_rows,
)


def get_manifest_stats(
    runner: "QueryRunner", parameters: ManifestStatsParameters | dict | None = None
):
    """Measure manifest statistics. Returns the stored ToolOutcome (Rxxx + payload)."""
    return runner.run(MANIFEST_STATS_SPEC.name, parameters)


__all__ = [
    "MANIFEST_STATS_SPEC",
    "ManifestStatsParameters",
    "ManifestStatsResult",
    "get_manifest_stats",
    "manifest_stats_from_rows",
]