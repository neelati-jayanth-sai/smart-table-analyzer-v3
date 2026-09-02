"""IOMETE / Spark backend (Runtime_Environments_UI.md #5-#7, #12).

Production queries use reviewed Spark/IOMETE-compatible SQL templates under
``execution/queries/iomete/``. The backend binds validated parameters into
the template and executes the resulting SQL through an ``IometeConnection``
seam. Tests stub that seam with deterministic fixture results, so the whole
tool layer stays testable without IOMETE network access or Docker.

The backend never branches on environment, never writes SQL, and never
diagnoses results. It returns normalized rows in the shared tool-contract
field names; ``sta.tools`` builds the final Pydantic payload.
"""

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from sta.context.table_metadata import TableMetadata
from sta.execution.backends.base import (
    BackendExecution,
    TableBackend,
    normalize_engine_rows,
)
from sta.execution.errors import (
    BackendExecutionError,
    BackendNotConfiguredError,
    ParameterValidationError,
    SnapshotNotAvailableError,
    TableNotResolvedError,
)
from sta.execution.queries.loader import bind_template, iomete_template_tools
from sta.tools.registry import DEFAULT_REGISTRY


@runtime_checkable
class IometeConnection(Protocol):
    """Production IOMETE execution seam."""

    def execute(self, sql: str) -> list[dict[str, Any]]: ...


class IometeBackend:
    """Production ``TableBackend`` for IOMETE / Spark / Iceberg.

    Parameters
    ----------
    table:
        Resolved catalog.namespace.table name.
    connection:
        ``IometeConnection`` that executes bound SQL and returns raw rows.
    maintenance_table:
        Optional catalog.namespace.table name for the maintenance configuration
        source. Required only for ``get_iomete_maintenance_config``.
    metadata:
        Optional backend-independent table metadata used to enrich result rows
        (e.g. partition-spec field lists). If omitted, enrichment columns such
        as ``fields`` on ``get_partition_spec_usage`` remain empty.
    """

    name = "iomete"

    def __init__(
        self,
        table: str,
        connection: IometeConnection,
        *,
        maintenance_table: str | None = None,
        metadata: TableMetadata | None = None,
    ) -> None:
        self._table = table
        self._connection = connection
        self._maintenance_table = maintenance_table
        self._metadata = metadata
        if connection is None:
            raise BackendNotConfiguredError("iomete", "no IOMETE connection provided")

    @property
    def table(self) -> str:
        return self._table

    def supported_tools(self) -> frozenset[str]:
        tools = iomete_template_tools()
        if not self._maintenance_table:
            tools = tools - {"get_iomete_maintenance_config"}
        return frozenset(tools)

    def load_table_metadata(self, table_name: str) -> TableMetadata:
        """Resolve a table name into backend-independent metadata.

        In production this delegates to the configured IOMETE catalog. For the
        MVP seam, metadata must be supplied at construction; a future adapter
        can replace this with a live catalog lookup.
        """
        if table_name != self._table:
            raise TableNotResolvedError(table_name)
        if self._metadata is None:
            raise BackendNotConfiguredError(
                "iomete", f"no metadata provider available for {table_name!r}"
            )
        return self._metadata

    def execute(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        snapshot_id: str | None,
    ) -> BackendExecution:
        if tool_name not in self.supported_tools():
            if tool_name in DEFAULT_REGISTRY:
                raise BackendExecutionError(
                    tool_name,
                    f"tool {tool_name!r} is not supported by the IOMETE backend "
                    "(missing reviewed template or maintenance table)",
                    retryable=False,
                )
            raise BackendExecutionError(
                tool_name, f"unknown tool {tool_name!r}", retryable=False
            )

        spec = DEFAULT_REGISTRY[tool_name]
        template_has_snapshot = _template_has_snapshot(tool_name)
        if template_has_snapshot and snapshot_id is None:
            raise SnapshotNotAvailableError(
                tool_name,
                "IOMETE backend requires a pinned snapshot for this tool",
            )

        bindings = self._build_bindings(tool_name, parameters, snapshot_id)
        sql = bind_template("iomete", tool_name, bindings)
        try:
            rows = self._connection.execute(sql)
        except ToolExecutionError:
            raise
        except Exception as exc:
            raise BackendExecutionError(
                tool_name,
                f"IOMETE execution failed ({type(exc).__name__})",
                retryable=True,
            ) from exc

        rows = normalize_engine_rows(rows)
        rows = self._enrich_rows(tool_name, rows)
        return BackendExecution(rows=rows, snapshot_id=snapshot_id if spec.snapshot_scoped else None)

    def _build_bindings(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        snapshot_id: str | None,
    ) -> dict[str, str]:
        placeholders = _template_placeholders(tool_name)
        bindings: dict[str, str] = {}

        if "table" in placeholders:
            bindings["table"] = self._table

        if tool_name == "get_iomete_maintenance_config":
            if self._maintenance_table is None:
                raise BackendExecutionError(
                    tool_name,
                    "maintenance_table is not configured",
                    retryable=False,
                )
            bindings["maintenance_table"] = self._maintenance_table

        if "limit" in placeholders:
            limit = parameters.get("limit")
            if limit is None:
                # Fall back to the parameter schema defaults.
                default = DEFAULT_REGISTRY[tool_name].parameters.model_fields.get("limit")
                limit = getattr(default, "default", 100) if default is not None else 100
            bindings["limit"] = str(int(limit))

        if "snapshot_id" in placeholders:
            if snapshot_id is None:
                raise SnapshotNotAvailableError(tool_name, "snapshot_id is required")
            bindings["snapshot_id"] = str(int(snapshot_id))

        if "column" in placeholders:
            column = parameters.get("column")
            if not isinstance(column, str):
                raise ParameterValidationError(tool_name, "column parameter is required")
            bindings["column"] = column

        if "field_id" in placeholders:
            field_id = self._resolve_field_id(parameters.get("column"))
            if field_id is None:
                raise ParameterValidationError(
                    tool_name,
                    f"column {parameters.get('column')!r} not found in current schema",
                )
            bindings["field_id"] = str(field_id)

        return bindings

    def _enrich_rows(self, tool_name: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if tool_name == "get_partition_spec_usage":
            return self._enrich_spec_usage(rows)
        return rows

    def _enrich_spec_usage(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add rendered partition fields from metadata when available."""
        if self._metadata is None or not self._metadata.partition_specs:
            return [{**row, "fields": []} for row in rows]
        fields_by_id: dict[int, str] = {}
        for schema in self._metadata.schemas:
            for field in schema.fields:
                fields_by_id[field.field_id] = field.name
        spec_fields = {
            spec.spec_id: [
                self._render_transform(field.transform, fields_by_id.get(field.source_id, field.name))
                for field in spec.fields
            ]
            for spec in (self._metadata.partition_specs or [])
        }
        return [
            {
                **row,
                "fields": spec_fields.get(int(row["spec_id"]), []),
            }
            for row in rows
        ]

    def _resolve_field_id(self, column: Any) -> int | None:
        if not isinstance(column, str) or self._metadata is None:
            return None
        current_schema_id = self._metadata.current_schema_id
        for schema in self._metadata.schemas:
            if schema.schema_id == current_schema_id:
                for field in schema.fields:
                    if field.name == column:
                        return field.field_id
        return None

    def _render_transform(self, transform: str, column: str) -> str:
        from sta.context.context_builder import render_transform  # noqa: PLC0415

        return render_transform(transform, column)


def _template_placeholders(tool_name: str) -> set[str]:
    from sta.execution.queries.loader import template_placeholders, load_template  # noqa: PLC0415

    return template_placeholders(load_template("iomete", tool_name))


def _template_has_snapshot(tool_name: str) -> bool:
    return "snapshot_id" in _template_placeholders(tool_name)


def _template_has_limit(tool_name: str) -> bool:
    return "limit" in _template_placeholders(tool_name)
