"""The backend-independent execution contract (Runtime_Environments_UI.md #4).

One logical ``TableBackend`` protocol, two implementations:

- ``sta.execution.backends.local.LocalIcebergBackend``   — local Docker Iceberg
  via PyIceberg + DuckDB (fixtures for tests, live adapters at the boundary),
- ``sta.execution.backends.iomete.IometeBackend``        — production IOMETE /
  Spark through reviewed query templates.

The Investigator and the QueryRunner never branch on the environment; they
only call this contract. Backends return *normalized* rows (plain dictionaries
in the tool's contract field names) so the single shared payload builder in
``sta.tools`` produces identical results everywhere.
"""

import datetime as _dt
import decimal
import re
import uuid
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTEGER = re.compile(r"^\d+$")


def is_identifier(value: str) -> bool:
    return bool(_IDENTIFIER.fullmatch(value))


def require_identifier(value: str, kind: str) -> str:
    if not is_identifier(value):
        raise ValueError(f"{kind} must be a plain identifier, got {value!r}")
    return value


def require_qualified_identifier(value: str, kind: str) -> str:
    """Validate a dot-separated SQL object name (catalog[.namespace][.table]).

    Every dot-separated part must be a plain identifier, so the joined value
    is safe to substitute into reviewed templates (no quoting/escaping
    tricks can survive per-part validation).
    """
    if not isinstance(value, str) or not 1 <= value.count(".") + 1 <= 3:
        raise ValueError(f"{kind} must be a dot-separated catalog.namespace.table name, got {value!r}")
    for part in value.split("."):
        require_identifier(part, kind)
    return value


def require_integer(value: str, kind: str) -> str:
    if not _INTEGER.fullmatch(value):
        raise ValueError(f"{kind} must be a non-negative integer, got {value!r}")
    return value


class BackendExecution(BaseModel):
    """Normalized query output: contract-shaped rows plus the snapshot scope
    actually measured. ``snapshot_id`` is None when the measurement is not
    snapshot-scoped — that absence is the explicit 'not pinned' mark
    (Architecture.md #15)."""

    rows: list[dict[str, Any]]
    snapshot_id: str | None = None


@runtime_checkable
class TableBackend(Protocol):
    """One logical backend contract.

    Backends are constructed per resolved table (``table`` is fixed), which
    keeps every tool call free of model-supplied table names. Resolution of a
    user table name into backend-independent metadata flows through the
    ``TableMetadataProvider`` seam in ``sta.context.table_metadata``.
    """

    name: str
    table: str

    def supported_tools(self) -> frozenset[str]: ...

    def execute(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        snapshot_id: str | None,
    ) -> BackendExecution: ...


def normalize_engine_value(value: Any) -> Any:
    """Normalize engine-specific Python types into contract-safe values.

    Applied by backend adapters to raw engine rows so the shared payload
    builder only ever sees plain scalars. Deterministic:
    - Decimal -> string (exact; no float drift in stored evidence),
    - date/datetime/time -> ISO-8601 string,
    - UUID -> string,
    - bytes -> lowercase hex (engine-encoded metadata such as Iceberg bounds),
    - anything else passes through unchanged.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, Mapping):
        return {str(key): normalize_engine_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [normalize_engine_value(item) for item in value]
    return value


def normalize_engine_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {key: normalize_engine_value(value) for key, value in dict(row).items()}
        for row in rows
    ]


__all__ = [
    "BackendExecution",
    "TableBackend",
    "is_identifier",
    "normalize_engine_rows",
    "normalize_engine_value",
    "require_identifier",
    "require_integer",
    "require_qualified_identifier",
]