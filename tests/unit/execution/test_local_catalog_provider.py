"""LocalCatalogProvider is fake/offline-testable through its ``catalog`` seam.

Runtime_Environments_UI.md §49, §54: configured catalog resolution must be
testable without live Docker. These tests exercise the identifier mapping and
metadata-delegation path using lightweight fakes; the fixture adapter path is
covered by the integration suite with injected components.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from sta.context.table_metadata import TableMetadata
from sta.execution.backends.local_catalog import LocalCatalogProvider
from sta.execution.errors import TableNotResolvedError


class _FakeMetadata:
    """Minimal metadata surface needed by ``table_metadata_from_pyiceberg``."""

    def __init__(self) -> None:
        self.format_version = 2
        self.schemas = [
            SimpleNamespace(
                schema_id=0,
                fields=[
                    SimpleNamespace(
                        field_id=1,
                        name="id",
                        field_type="long",
                        required=True,
                        doc=None,
                    )
                ],
            )
        ]
        self.current_schema_id = 0
        self.current_snapshot_id = 123456789
        self.partition_specs = []
        self.default_spec_id = 0
        self.sort_orders = []
        self.default_sort_order_id = 0
        self.properties = {}


class _FakeTable:
    """Minimal PyIceberg ``Table`` surface used by the adapter."""

    def __init__(self, identifier: tuple[str, ...]) -> None:
        self._identifier = identifier
        self.metadata = _FakeMetadata()

    def name(self) -> tuple[str, ...]:
        return self._identifier

    def schema(self) -> Any:
        return SimpleNamespace(fields=self.metadata.schemas[0].fields)


class _FakeCatalog:
    """In-memory catalog: maps identifiers to fake tables."""

    def __init__(self, tables: dict[str, Any]) -> None:
        self._tables = tables
        self.calls: list[str] = []

    def load_table(self, identifier: str) -> Any:
        self.calls.append(identifier)
        if identifier not in self._tables:
            raise TableNotResolvedError(identifier)
        return self._tables[identifier]


def test_load_table_metadata_uses_configured_catalog_name() -> None:
    """A three-part name prefixed with the configured catalog maps to the
    REST namespace/table identifier, and the metadata carries the canonical
    catalog.schema.table identity. The fake catalog proves offline use."""
    table = _FakeTable(("demo", "sales"))
    catalog = _FakeCatalog({"demo.sales": table})
    provider = LocalCatalogProvider(catalog, catalog_name="local")

    metadata = provider.load_table_metadata("local.demo.sales")

    assert isinstance(metadata, TableMetadata)
    assert metadata.table == "local.demo.sales"
    assert metadata.current_snapshot_id == 123456789
    assert catalog.calls == ["demo.sales"]


def test_wrong_catalog_name_rejects_resolution() -> None:
    """Tables outside the configured catalog fail fast."""
    table = _FakeTable(("demo", "sales"))
    catalog = _FakeCatalog({"demo.sales": table})
    provider = LocalCatalogProvider(catalog, catalog_name="local")

    with pytest.raises(TableNotResolvedError, match="catalog 'production' is not configured"):
        provider.load_table_metadata("production.demo.sales")


def test_short_identifier_passes_through() -> None:
    """A two-part namespace.table name is forwarded unchanged to the catalog
    and canonicalized under the configured catalog in the metadata."""
    table = _FakeTable(("demo", "sales"))
    catalog = _FakeCatalog({"demo.sales": table})
    provider = LocalCatalogProvider(catalog, catalog_name="local")

    metadata = provider.load_table_metadata("demo.sales")

    assert catalog.calls == ["demo.sales"]
    assert metadata.table == "local.demo.sales"


def test_unqualified_table_name_rejects_resolution() -> None:
    """Single-part names are not valid table references."""
    provider = LocalCatalogProvider(_FakeCatalog({}), catalog_name="local")

    with pytest.raises(TableNotResolvedError, match="table must be a catalog.namespace.table name"):
        provider.load_table_metadata("sales")
