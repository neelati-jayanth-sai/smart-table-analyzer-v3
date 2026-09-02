"""Execution layer: QueryRunner, backend seams, and reviewed query templates."""

from sta.execution.backends.base import (
    BackendExecution,
    TableBackend,
    is_identifier,
    normalize_engine_rows,
    normalize_engine_value,
    require_identifier,
    require_integer,
    require_qualified_identifier,
)
from sta.execution.errors import (
    BackendExecutionError,
    BackendNotConfiguredError,
    ParameterValidationError,
    QueryTimeoutError,
    SnapshotNotAvailableError,
    TableNotResolvedError,
    ToolExecutionError,
    UnknownToolError,
    UnsupportedToolError,
)
from sta.execution.queries.loader import (
    bind_template,
    iomete_template_tools,
    load_template,
    template_placeholders,
)
from sta.execution.runner import QueryRunner, ToolOutcome

__all__ = [
    "BackendExecution",
    "BackendExecutionError",
    "BackendNotConfiguredError",
    "ParameterValidationError",
    "QueryRunner",
    "QueryTimeoutError",
    "SnapshotNotAvailableError",
    "TableBackend",
    "TableNotResolvedError",
    "ToolExecutionError",
    "ToolOutcome",
    "UnknownToolError",
    "UnsupportedToolError",
    "bind_template",
    "iomete_template_tools",
    "is_identifier",
    "load_template",
    "normalize_engine_rows",
    "normalize_engine_value",
    "require_identifier",
    "require_integer",
    "require_qualified_identifier",
    "template_placeholders",
]
