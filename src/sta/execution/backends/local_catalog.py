"""Local PyIceberg catalog provider — the live local-environment seam
(Runtime_Environments_UI.md #8-#13, #49).

Wires the configured local Iceberg REST catalog (Docker) into STA's two
backend seams:

- :class:`TableMetadataProvider` — resolves a user table name into
  backend-independent :class:`~sta.context.table_metadata.TableMetadata`
  through PyIceberg,
- ``RunComponents.backend_factory`` — builds the run's
  :class:`~sta.execution.backends.local.LocalIcebergBackend` from the live
  PyIceberg table (metadata, manifests, files, column metrics) via
  :mod:`sta.execution.backends.pyiceberg_adapter`.

PyIceberg is imported lazily at this boundary only; every other STA module
consumes normalized fixtures. The provider performs no interpretation and no
data reads beyond Iceberg metadata (§10). DuckDB is only touched through the
``analyze_partition_candidate`` profile-provider seam.

Fail-closed behaviour (§49, Architecture.md #36):

- a table outside the configured catalog raises ``TableNotResolvedError``,
- catalog/network failures raise a typed error with a safe message (never
  connection details or credentials),
- missing local configuration is rejected at startup by
  :meth:`sta.config.Settings.validate_environment`.
"""

import logging
from collections.abc import Callable
from typing import Any

from sta.config import Settings
from sta.context.table_metadata import TableMetadata
from sta.execution.backends.duckdb_candidate import DuckDbCandidateProfileProvider
from sta.execution.backends.local import LocalIcebergBackend, LocalTableFixture
from sta.execution.backends.pyiceberg_adapter import (
    table_fixture_from_pyiceberg,
    table_metadata_from_pyiceberg,
)
from sta.execution.errors import BackendExecutionError, TableNotResolvedError

logger = logging.getLogger(__name__)

# PyIceberg raises these for names that do not resolve; every other failure
# is a typed catalog-availability error with a safe message.
try:  # pragma: no cover - import shape depends on the PyIceberg build
    from pyiceberg.exceptions import NoSuchNamespaceError, NoSuchTableError

    _TABLE_NOT_FOUND_ERRORS: tuple[type[Exception], ...] = (NoSuchTableError, NoSuchNamespaceError)
except ImportError:  # pragma: no cover
    _TABLE_NOT_FOUND_ERRORS = ()


def load_local_catalog(settings: Settings) -> Any:
    """Build the PyIceberg catalog client from validated local settings.

    ``load_catalog`` reads the REST server's config on construction, so an
    unreachable catalog fails here — at startup, per §49. Import and
    construction errors surface as a typed, safe :class:`BackendExecutionError`
    (exception type name only; no connection details, no credentials).
    """
    properties = settings.pyiceberg_properties()
    try:
        from pyiceberg.catalog import load_catalog as pyiceberg_load_catalog
    except ImportError as exc:  # pragma: no cover - pyiceberg is a hard dependency
        raise BackendExecutionError(
            "local-catalog",
            "PyIceberg is not installed; install the local environment dependencies",
        ) from exc
    try:
        return pyiceberg_load_catalog(settings.iceberg_catalog_name, **properties)
    except Exception as exc:  # noqa: BLE001 - catalog construction is typed, safe
        raise BackendExecutionError(
            "local-catalog",
            f"could not connect to the configured local Iceberg catalog ({type(exc).__name__}); "
            "check ICEBERG_CATALOG_URI and docker compose up -d",
        ) from exc


class LocalCatalogProvider:
    """Resolves tables in the configured local catalog and builds the local
    backend. One instance per application; safe to share across runs.

    ``catalog`` is any object exposing the PyIceberg ``Catalog`` surface used
    here (``load_table``) — tests inject fakes, deployments build one with
    :func:`load_local_catalog`.
    """

    name = "local"

    def __init__(
        self,
        catalog: Any,
        *,
        catalog_name: str = "local",
        candidate_profile_provider_factory: Callable[[LocalTableFixture], Any] | None = None,
    ) -> None:
        self._catalog = catalog
        self._catalog_name = catalog_name
        self._candidate_profile_provider_factory = candidate_profile_provider_factory

    @classmethod
    def from_settings(cls, settings: Settings) -> "LocalCatalogProvider":
        """Build a provider against the configured local REST catalog.

        Raises a typed, safe error when the catalog cannot be constructed
        (missing configuration must fail clearly, never silently)."""
        catalog = load_local_catalog(settings)
        return cls(
            catalog,
            catalog_name=settings.iceberg_catalog_name,
            candidate_profile_provider_factory=lambda fixture: DuckDbCandidateProfileProvider(
                fixture, s3_properties=settings.s3_properties()
            ),
        )

    # -- TableMetadataProvider seam ------------------------------------------

    def load_table_metadata(self, table_name: str) -> TableMetadata:
        """Resolve ``catalog.namespace.table`` through the configured catalog.

        The returned metadata carries the canonical three-part name the user
        asked for (``catalog`` prefix included): one identity is preserved
        through metadata, fixture, results, report and the run record."""
        canonical = self._canonical_name(table_name)
        table = self._load(table_name)
        metadata = table_metadata_from_pyiceberg(table)
        return metadata.model_copy(update={"table": canonical})

    # -- backend factory seam --------------------------------------------------

    def create_backend(self, table: str, metadata: TableMetadata) -> LocalIcebergBackend:
        """Build the run backend from the live table's PyIceberg metadata.

        The table is loaded again for the fixture build so the backend always
        reflects one consistent snapshot; a commit landing between metadata
        resolution and backend construction surfaces as a typed snapshot error
        instead of mixed evidence (Architecture.md #15). The fixture is
        canonicalized to the resolved catalog.namespace.table identity so the
        backend, results and report all carry the same table name.
        """
        canonical = self._canonical_name(table)
        if metadata.table != canonical:
            raise TableNotResolvedError(
                table,
                f"metadata table {metadata.table!r} does not match the resolved "
                f"table {canonical!r}",
            )
        py_table = self._load(table)
        fixture = table_fixture_from_pyiceberg(py_table)
        fixture = fixture.model_copy(update={"table": canonical})
        if _snapshot_key(fixture.snapshot_id) != _snapshot_key(metadata.current_snapshot_id):
            raise BackendExecutionError(
                "table-resolution",
                "the table changed between resolution and backend construction; retry the run",
            )
        provider = (
            self._candidate_profile_provider_factory(fixture)
            if self._candidate_profile_provider_factory is not None
            else None
        )
        return LocalIcebergBackend(fixture, candidate_profile_provider=provider)

    # -- internals ---------------------------------------------------------------

    def _load(self, table_name: str) -> Any:
        try:
            return self._catalog.load_table(self._identifier(self._canonical_name(table_name)))
        except TableNotResolvedError:
            raise
        except _TABLE_NOT_FOUND_ERRORS as exc:
            raise TableNotResolvedError(
                table_name, f"table {table_name!r} was not found in the configured local catalog"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - never leak connection details
            logger.warning("local catalog lookup failed for %s: %s", table_name, type(exc).__name__)
            raise BackendExecutionError(
                table_name,
                f"local catalog lookup failed ({type(exc).__name__}); "
                "check that the local Iceberg catalog is running",
            ) from exc

    def _canonical_name(self, table_name: str) -> str:
        """Canonical ``catalog.namespace.table`` identity for a user name.

        A three-part name must prefix the configured catalog (Architecture.md
        #36: the table must resolve inside allowed catalogs) and is canonical
        as-is; a two-part name is canonicalized under the configured catalog;
        anything else is rejected."""
        parts = table_name.split(".")
        if len(parts) == 2:
            return f"{self._catalog_name}.{table_name}"
        if len(parts) == 3:
            if parts[0] != self._catalog_name:
                raise TableNotResolvedError(
                    table_name,
                    f"catalog {parts[0]!r} is not configured; this environment "
                    f"resolves tables in catalog {self._catalog_name!r}",
                )
            return table_name
        raise TableNotResolvedError(
            table_name, "table must be a catalog.namespace.table name"
        )

    def _identifier(self, canonical: str) -> str:
        """Map the canonical ``catalog.namespace.table`` onto the PyIceberg
        identifier this catalog serves (REST catalogs are the namespace root)."""
        parts = canonical.split(".")
        return ".".join(parts[1:]) if len(parts) == 3 else canonical


def _snapshot_key(snapshot_id: int | str | None) -> str | None:
    """Comparable snapshot identity; ``None`` (no snapshots) stays ``None``
    instead of being normalized into a fabricated "0"."""
    return None if snapshot_id is None else str(snapshot_id)


__all__ = ["LocalCatalogProvider", "load_local_catalog"]