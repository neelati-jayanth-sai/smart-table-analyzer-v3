"""Reviewed query templates (Architecture.md #12, Runtime_Environments_UI.md
#7, #13).

``queries/iomete/`` holds the checked-in Spark SQL templates for the IOMETE
backend; ``queries/local/`` holds the single fixed DuckDB template used by the
local targeted-analysis seam. Templates are static application code: the LLM
never sees, edits or generates them, and the loader below refuses anything
that is not a checked-in, placeholder-integrity-verified file.

Placeholder rules (enforced at bind time):

- ``:table`` / ``:maintenance_table`` — dot-separated identifiers
  (catalog[.namespace][.table], every part validated),
- ``:column`` — a plain SQL identifier,
- ``:snapshot_id`` / ``:limit`` / ``:field_id`` — non-negative integers,
- ``:source`` — an internally rendered scan source (local DuckDB seam only;
  built from infrastructure paths, never from model input).
"""

import importlib.resources
import re

from sta.execution.backends.base import (
    require_identifier,
    require_integer,
    require_qualified_identifier,
)
from sta.execution.errors import BackendExecutionError

_PLACEHOLDER = re.compile(r":([a-z_][a-z0-9_]*)")

_BINDERS = {
    # Table names are configured/resolved values (catalog.namespace.table);
    # each dot-separated part is validated as a plain identifier.
    "table": require_qualified_identifier,
    "maintenance_table": require_qualified_identifier,
    "column": require_identifier,
    "snapshot_id": require_integer,
    "limit": require_integer,
    "field_id": require_integer,
}

# The only free-form placeholder; bound from internally built scan sources.
_FREE_FORM = frozenset({"source"})

_TEMPLATES: dict[tuple[str, str], str] = {}


def load_template(environment: str, tool_name: str) -> str:
    """Load the reviewed template for (environment, tool). Templates are
    package data — one static file per tool, reviewed in code review."""
    key = (environment, tool_name)
    cached = _TEMPLATES.get(key)
    if cached is not None:
        return cached
    path = importlib.resources.files("sta.execution.queries").joinpath(
        environment, f"{tool_name}.sql"
    )
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise BackendExecutionError(
            tool_name, f"no reviewed query template is available for {tool_name!r}"
        ) from exc
    _TEMPLATES[key] = text
    return text


def template_placeholders(template: str) -> set[str]:
    return set(_PLACEHOLDER.findall(template))


def bind_template(environment: str, tool_name: str, bindings: dict[str, str]) -> str:
    """Validate every binding and substitute it into the reviewed template.

    Fails closed: an unbound template placeholder or an invalid value is an
    implementation/configuration error, never an executed query.
    """
    template = load_template(environment, tool_name)
    placeholders = template_placeholders(template)
    unknown = placeholders - set(_BINDERS) - _FREE_FORM
    if unknown:
        raise BackendExecutionError(
            tool_name, f"template references unbindable placeholders: {sorted(unknown)}"
        )
    missing = placeholders - set(bindings)
    if missing:
        raise BackendExecutionError(
            tool_name, f"template is missing required bindings: {sorted(missing)}"
        )
    extra = set(bindings) - placeholders
    if extra:
        raise BackendExecutionError(
            tool_name, f"bindings not present in the template: {sorted(extra)}"
        )

    bound = template
    for name, value in sorted(bindings.items()):
        if name in _FREE_FORM:
            if not isinstance(value, str) or any(marker in value for marker in (";", "--", "/*")):
                raise BackendExecutionError(tool_name, f"invalid {name} binding")
        else:
            try:
                value = _BINDERS[name](value, name)
            except ValueError as exc:
                raise BackendExecutionError(tool_name, str(exc)) from exc
        bound = bound.replace(f":{name}", value)
    return bound


def iomete_template_tools() -> set[str]:
    """Tool names that have a checked-in IOMETE template."""
    root = importlib.resources.files("sta.execution.queries").joinpath("iomete")
    return {
        entry.name[: -len(".sql")] for entry in root.iterdir() if entry.name.endswith(".sql")
    }