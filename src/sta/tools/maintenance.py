"""IOMETE maintenance-configuration tool (Architecture.md #13
``get_iomete_maintenance_config``).

Returns effective/discoverable maintenance configuration as raw key/value
entries, without interpreting it. The tool is available only where the
deployment exposes a configuration source: on the IOMETE backend when a
maintenance configuration table is configured, and not at all on the local
Docker backend (Runtime_Environments_UI.md #5). Values are measured, never
evaluated.
"""

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from sta.tools.spec import ToolSpec

if TYPE_CHECKING:
    from sta.execution.runner import QueryRunner


class MaintenanceConfigParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MaintenanceConfigEntry(BaseModel):
    key: str
    value: str
    source: str


class MaintenanceConfigResult(BaseModel):
    available: bool
    entries: list[MaintenanceConfigEntry]


def maintenance_config_from_rows(
    rows: list[dict], _params: MaintenanceConfigParameters
) -> MaintenanceConfigResult:
    entries = [MaintenanceConfigEntry.model_validate(dict(row)) for row in rows]
    entries.sort(key=lambda entry: (entry.source, entry.key))
    return MaintenanceConfigResult(available=bool(entries), entries=entries)


MAINTENANCE_CONFIG_SPEC = ToolSpec(
    name="get_iomete_maintenance_config",
    query_version="iomete_maintenance_config:v1",
    description=(
        "IOMETE-only: returns discoverable maintenance configuration entries "
        "(key, value, source) without interpreting them. Unavailable on "
        "local deployments."
    ),
    parameters=MaintenanceConfigParameters,
    result=MaintenanceConfigResult,
    build_payload=maintenance_config_from_rows,
    snapshot_scoped=False,
    entry_model=MaintenanceConfigEntry,
    rows_field="entries",
)


def get_iomete_maintenance_config(
    runner: "QueryRunner", parameters: MaintenanceConfigParameters | dict | None = None
):
    """Read maintenance configuration if available. Returns the stored ToolOutcome."""
    return runner.run(MAINTENANCE_CONFIG_SPEC.name, parameters)