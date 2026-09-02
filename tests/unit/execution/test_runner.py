"""QueryRunner tests (Architecture.md #12, #15, #17, #35).

Covers result persistence (Rxxx), event emission, snapshot scope,
typed failures, timeout and one deterministic retry."""

import time
from collections.abc import Mapping
from typing import Any

import pytest

from sta.execution.backends.base import BackendExecution
from sta.execution.backends.local import LocalIcebergBackend, LocalTableFixture
from sta.execution.errors import (
    BackendExecutionError,
    ParameterValidationError,
    QueryTimeoutError,
    SnapshotNotAvailableError,
    UnsupportedToolError,
)
from sta.execution.runner import QueryRunner
from sta.results.models import RunRecord
from sta.results.store import ResultStore


def _store(tmp_path) -> ResultStore:
    return ResultStore(tmp_path / "sta.sqlite3")


def _create_run(store: ResultStore, run_id: str, table: str) -> str:
    return store.create_run(
        RunRecord(run_id=run_id, table=table, started_at="2024-01-01T00:00:00", status="running")
    ).run_id


def test_runner_stores_result_as_r001(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_1", fixture.table)

    with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
        outcome = runner.run("get_file_layout")

    assert outcome.result_id == "R001"
    assert outcome.tool_name == "get_file_layout"
    assert outcome.snapshot_id == str(fixture.snapshot_id)
    stored = store.get_result(run_id, "R001")
    assert stored is not None
    assert stored.tool_name == "get_file_layout"
    assert stored.row_count == 1
    assert stored.payload is not None


def test_runner_emits_progress_events(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_e", fixture.table)

    with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
        runner.run("get_file_layout")

    events = store.list_events(run_id)
    types = [event.type for event in events]
    assert "tool_requested" in types
    assert "query_started" in types
    assert "result_stored" in types


def test_snapshot_scoped_tools_record_snapshot_id(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_s", fixture.table)

    with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
        outcome = runner.run("get_file_layout")
        assert outcome.snapshot_id == str(fixture.snapshot_id)

        outcome_hist = runner.run("get_snapshot_history")
        assert outcome_hist.snapshot_id is None


def test_unscoped_tools_work_without_snapshot(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_u", fixture.table)

    with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
        outcome = runner.run("get_snapshot_history")
        assert outcome.snapshot_id is None


def test_parameter_validation_failure_emits_events_and_raises(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_p", fixture.table)

    with pytest.raises(ParameterValidationError):
        with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
            runner.run("analyze_partition_candidate", {"column": "order_id"})

    events = store.list_events(run_id)
    assert any(event.type == "tool_failed" for event in events)
    assert not store.list_results(run_id)


def test_unknown_tool_raises(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_unk", fixture.table)

    with pytest.raises(Exception) as exc_info:
        with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
            runner.run("no_such_tool")
    assert "unknown" in str(exc_info.value).lower()


def test_unsupported_tool_raises(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    """Local backend does not support get_iomete_maintenance_config."""
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_uns", fixture.table)

    with pytest.raises(UnsupportedToolError):
        with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id=str(fixture.snapshot_id)) as runner:
            runner.run("get_iomete_maintenance_config")


class _RetryOnceBackend:
    name = "retry_once"
    table = "demo.sales.orders"

    def __init__(self) -> None:
        self._calls = 0
        self._fail_first = True

    def supported_tools(self) -> frozenset[str]:
        return frozenset({"get_file_layout"})

    def execute(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        snapshot_id: str | None,
    ) -> BackendExecution:
        self._calls += 1
        if self._fail_first:
            self._fail_first = False
            raise BackendExecutionError(tool_name, "transient", retryable=True)
        return BackendExecution(rows=[{"file_count": 1, "total_size_bytes": 100}], snapshot_id=snapshot_id)


def test_retryable_failure_is_retried_once(tmp_path) -> None:
    backend = _RetryOnceBackend()
    store = _store(tmp_path)
    run_id = _create_run(store, "run_r", backend.table)

    with QueryRunner(backend=backend, store=store, run_id=run_id, table=backend.table, pinned_snapshot_id="123") as runner:
        outcome = runner.run("get_file_layout")

    assert backend._calls == 2
    assert outcome.result_id == "R001"


class _AlwaysFailBackend:
    name = "always_fail"
    table = "demo.sales.orders"

    def __init__(self) -> None:
        self.calls = 0

    def supported_tools(self) -> frozenset[str]:
        return frozenset({"get_file_layout"})

    def execute(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        snapshot_id: str | None,
    ) -> BackendExecution:
        self.calls += 1
        raise BackendExecutionError(tool_name, "permanent", retryable=False)


def test_non_retryable_failure_is_not_retried(tmp_path) -> None:
    backend = _AlwaysFailBackend()
    store = _store(tmp_path)
    run_id = _create_run(store, "run_nr", backend.table)

    with pytest.raises(BackendExecutionError):
        with QueryRunner(backend=backend, store=store, run_id=run_id, table=backend.table, pinned_snapshot_id="123") as runner:
            runner.run("get_file_layout")

    assert backend.calls == 1


class _SleepingBackend:
    name = "sleep"
    table = "demo.sales.orders"

    def supported_tools(self) -> frozenset[str]:
        return frozenset({"get_file_layout"})

    def execute(
        self,
        tool_name: str,
        parameters: Mapping[str, Any],
        snapshot_id: str | None,
    ) -> BackendExecution:
        time.sleep(2.0)
        return BackendExecution(rows=[{"file_count": 1, "total_size_bytes": 100}], snapshot_id=snapshot_id)


def test_timeout_raises_query_timeout(tmp_path) -> None:
    backend = _SleepingBackend()
    store = _store(tmp_path)
    run_id = _create_run(store, "run_to", backend.table)

    with pytest.raises(QueryTimeoutError):
        with QueryRunner(backend=backend, store=store, run_id=run_id, table=backend.table, pinned_snapshot_id="123", timeout_seconds=0.1) as runner:
            runner.run("get_file_layout")


def test_snapshot_mismatch_raises(tmp_path, local_table_fixture: LocalTableFixture) -> None:
    fixture = local_table_fixture
    backend = LocalIcebergBackend(fixture)
    store = _store(tmp_path)
    run_id = _create_run(store, "run_mis", fixture.table)

    with pytest.raises(SnapshotNotAvailableError):
        with QueryRunner(backend=backend, store=store, run_id=run_id, table=fixture.table, pinned_snapshot_id="123") as runner:
            runner.run("get_file_layout")
