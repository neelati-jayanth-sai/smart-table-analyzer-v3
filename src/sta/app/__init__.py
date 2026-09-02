"""FastAPI application: HTTP API, run lifecycle, SSE progress, static UI
(Runtime_Environments_UI.md #18, #29, #30, #60)."""

from sta.app.api import create_app, default_components, validate_settings
from sta.app.events import event_stream, format_sse_event, parse_last_event_id
from sta.app.runs import (
    TERMINAL_STATUSES,
    RunComponents,
    RunService,
    RunCancelledError,
    UnconfiguredMetadataProvider,
    unconfigured_backend_factory,
)

__all__ = [
    "TERMINAL_STATUSES",
    "RunCancelledError",
    "RunComponents",
    "RunService",
    "UnconfiguredMetadataProvider",
    "create_app",
    "default_components",
    "event_stream",
    "format_sse_event",
    "parse_last_event_id",
    "unconfigured_backend_factory",
    "validate_settings",
]