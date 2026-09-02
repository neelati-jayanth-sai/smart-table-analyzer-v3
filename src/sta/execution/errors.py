"""Typed tool-execution failures (Architecture.md #35).

Every failure carries the tool, a stable error class, a safe message and a
retryable flag. Safe means: never SQL text, never connection details, never
credentials (Runtime_Environments_UI.md #6, #23). Because SQL is predefined,
persistent query failures are implementation/platform issues, not reasoning
opportunities — failures surface as typed values for the Investigator and the
run lifecycle.
"""


class ToolExecutionError(Exception):
    """Base class for deterministic tool failures."""

    error_class = "tool_error"

    def __init__(self, tool_name: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.tool_name = tool_name
        self.message = message
        self.retryable = retryable

    def __str__(self) -> str:
        return f"{self.tool_name}: {self.message}"


class UnknownToolError(ToolExecutionError):
    error_class = "unknown_tool"

    def __init__(self, tool_name: str):
        super().__init__(tool_name, f"unknown tool {tool_name!r}", retryable=False)


class UnsupportedToolError(ToolExecutionError):
    error_class = "unsupported_tool"

    def __init__(self, tool_name: str, backend_name: str):
        super().__init__(
            tool_name,
            f"tool {tool_name!r} is not available on backend {backend_name!r}",
            retryable=False,
        )


class ParameterValidationError(ToolExecutionError):
    error_class = "invalid_parameters"


class TableNotResolvedError(ToolExecutionError):
    """Raised by backends when a table name does not resolve inside the
    configured allowed catalogs/namespaces (Architecture.md #36)."""

    error_class = "table_not_resolved"

    def __init__(self, table_name: str, reason: str = "table does not resolve inside the configured catalogs/namespaces"):
        super().__init__(table_name, reason, retryable=False)


class BackendNotConfiguredError(ToolExecutionError):
    """A backend was used without its required deployment configuration
    (e.g. the IOMETE backend without a configured transport). Failing closed
    keeps an unconfigured backend from executing anything."""

    error_class = "backend_not_configured"

    def __init__(self, backend_name: str, message: str):
        super().__init__(backend_name, message, retryable=False)


class SnapshotNotAvailableError(ToolExecutionError):
    """The backend cannot serve the requested snapshot (e.g. the local fixture
    represents a different state). Snapshot consistency is never silently
    violated (Architecture.md #15)."""

    error_class = "snapshot_not_available"


class QueryTimeoutError(ToolExecutionError):
    error_class = "query_timeout"

    def __init__(self, tool_name: str, timeout_seconds: float):
        super().__init__(
            tool_name,
            f"execution exceeded the {timeout_seconds:g}s query timeout",
            retryable=True,
        )


class BackendExecutionError(ToolExecutionError):
    error_class = "backend_execution_failed"

    def __init__(self, tool_name: str, message: str, *, retryable: bool = False):
        super().__init__(tool_name, message, retryable=retryable)