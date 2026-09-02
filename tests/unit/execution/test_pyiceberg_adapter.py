"""PyIceberg adapter tests (Runtime_Environments_UI.md #8-#13).

The adapter is the only STA module touching PyIceberg. These tests cover the
pure mappers with the real installed PyIceberg objects plus lightweight
fakes:

- snapshot summaries are read through the public PyIceberg surface via the
  :func:`summary_properties` compatibility helper (no unconditional private
  attribute reliance),
- an empty table (no snapshots) converts to ``snapshot_id=None`` — snapshot
  0 is never fabricated.
"""

from types import SimpleNamespace
from typing import Any

from pyiceberg.table.snapshots import Operation, Summary

from sta.execution.backends.pyiceberg_adapter import (
    local_snapshots,
    summary_properties,
    table_fixture_from_pyiceberg,
)


class _PublicOnlySummary:
    """Fake summary exposing only the public accessor."""

    def __init__(self) -> None:
        self._properties = {"added-records": "7", "total-data-files": "2"}

    def additional_properties(self) -> dict[str, str]:
        return self._properties


class _LegacySummary:
    """Fake summary shaped like older PyIceberg builds: private attribute
    only, no public accessor."""

    def __init__(self) -> None:
        self._additional_properties = {"added-records": "3"}


class _BareSummary:
    """A summary that exposes neither accessor."""


class _FakeMetadata:
    """Minimal PyIceberg ``TableMetadata`` surface used by the mappers."""

    def __init__(self, snapshot_id: int | None, snapshots: list[Any]) -> None:
        self.format_version = 2
        from types import SimpleNamespace

        self.schemas = [
            SimpleNamespace(
                schema_id=0,
                fields=[
                    SimpleNamespace(
                        field_id=1, name="id", field_type="long", required=True, doc=None
                    )
                ],
            )
        ]
        self.current_schema_id = 0
        self.current_snapshot_id = snapshot_id
        self.partition_specs = []
        self.default_spec_id = 0
        self.sort_orders = []
        self.default_sort_order_id = 0
        self.properties = {}
        self.snapshots = snapshots


class _FakeTable:
    def __init__(self, metadata: _FakeMetadata) -> None:
        self.metadata = metadata
        self.io = object()

    def name(self) -> tuple[str, ...]:
        return ("demo", "sales")

    def schema(self) -> Any:
        from types import SimpleNamespace

        return SimpleNamespace(fields=self.metadata.schemas[0].fields)


def test_summary_properties_reads_real_pyiceberg_summary() -> None:
    """The public accessor of the installed PyIceberg build is used."""
    summary = Summary(
        operation=Operation.APPEND,
        **{"added-data-files": "3", "added-records": "300"},
    )

    properties = summary_properties(summary)

    assert properties == {"added-data-files": "3", "added-records": "300"}
    # The private attribute is never the first choice when the public
    # accessor exists.
    assert "operation" not in properties


def test_summary_properties_prefers_public_accessor() -> None:
    assert summary_properties(_PublicOnlySummary()) == {
        "added-records": "7",
        "total-data-files": "2",
    }


def test_summary_properties_falls_back_for_legacy_builds() -> None:
    assert summary_properties(_LegacySummary()) == {"added-records": "3"}


def test_summary_properties_without_any_accessor_yields_empty() -> None:
    assert summary_properties(_BareSummary()) == {}


def test_local_snapshots_converts_real_summary() -> None:
    snapshot = SimpleNamespace(
        snapshot_id=42,
        parent_snapshot_id=None,
        timestamp_ms=1700000000000,
        summary=Summary(operation=Operation.APPEND, **{"added-data-files": "1"}),
    )

    rows = local_snapshots([snapshot])

    assert len(rows) == 1
    assert rows[0].snapshot_id == 42
    assert rows[0].operation == "append"
    assert rows[0].summary == {"added-data-files": "1"}


def test_empty_table_fixture_preserves_snapshot_none() -> None:
    """A table without snapshots converts with ``snapshot_id=None``; the
    adapter never fabricates snapshot 0 and reads no manifests."""
    table = _FakeTable(_FakeMetadata(snapshot_id=None, snapshots=[]))

    fixture = table_fixture_from_pyiceberg(table)

    assert fixture.snapshot_id is None
    assert fixture.snapshots == []
    assert fixture.manifests == []
    assert fixture.data_files == []


def test_fixture_uses_pyiceberg_table_name() -> None:
    """The adapter keeps the PyIceberg-resolved name; the catalog provider is
    responsible for the canonical catalog-prefixed identity."""
    snapshot = SimpleNamespace(
        snapshot_id=9,
        parent_snapshot_id=None,
        timestamp_ms=1700000000000,
        summary=Summary(operation=Operation.APPEND),
        manifests=lambda io: [],
    )
    table = _FakeTable(_FakeMetadata(snapshot_id=9, snapshots=[snapshot]))

    fixture = table_fixture_from_pyiceberg(table)

    assert fixture.table == "demo.sales"
    assert fixture.snapshot_id == 9