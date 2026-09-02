"""Backend seams: local (PyIceberg/DuckDB) and production (IOMETE/Spark)."""

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
from sta.execution.backends.duckdb_candidate import DuckDbCandidateProfileProvider
from sta.execution.backends.iomete import IometeBackend, IometeConnection
from sta.execution.backends.local import (
    CandidateProfileProvider,
    LocalColumnMetrics,
    LocalColumnProfile,
    LocalFileEntry,
    LocalIcebergBackend,
    LocalManifestEntry,
    LocalSnapshot,
    LocalTableFixture,
)
from sta.execution.backends.local_catalog import LocalCatalogProvider, load_local_catalog

__all__ = [
    "BackendExecution",
    "CandidateProfileProvider",
    "DuckDbCandidateProfileProvider",
    "IometeBackend",
    "IometeConnection",
    "LocalCatalogProvider",
    "LocalColumnMetrics",
    "LocalColumnProfile",
    "LocalFileEntry",
    "LocalIcebergBackend",
    "LocalManifestEntry",
    "LocalSnapshot",
    "LocalTableFixture",
    "TableBackend",
    "is_identifier",
    "load_local_catalog",
    "normalize_engine_rows",
    "normalize_engine_value",
    "require_identifier",
    "require_integer",
    "require_qualified_identifier",
]
