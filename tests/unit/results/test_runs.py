"""Persistence tests for runs and reports (Architecture.md #33)."""

import pytest
from pydantic import BaseModel

from sta.results import ResultStore, RunRecord, new_run_id


def make_store(tmp_path) -> ResultStore:
    return ResultStore(tmp_path / "sta.sqlite3")


def make_run(**overrides) -> RunRecord:
    fields = {"run_id": new_run_id(), "table": "prod.sales.orders"}
    fields.update(overrides)
    return RunRecord(**fields)


def test_create_and_get_run_round_trip(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run(snapshot_id="918278128", status="running", phase="investigating")
        store.create_run(run)

        loaded = store.get_run(run.run_id)

        assert loaded == run


def test_get_missing_run_returns_none(tmp_path):
    with make_store(tmp_path) as store:
        assert store.get_run("run_unknown") is None


def test_create_run_requires_run_id(tmp_path):
    with make_store(tmp_path) as store:
        with pytest.raises(ValueError):
            store.create_run(RunRecord(run_id="", table="prod.sales.orders"))


def test_update_run_partial_fields(tmp_path):
    with make_store(tmp_path) as store:
        run = store.create_run(make_run(phase="building_context"))

        updated = store.update_run(run.run_id, status="running", phase="investigating")

        assert updated.status == "running"
        assert updated.phase == "investigating"
        assert updated.snapshot_id is None
        assert updated.completed_at is None


def test_update_run_marks_completion(tmp_path):
    with make_store(tmp_path) as store:
        run = store.create_run(make_run(status="running"))
        completed_at = "2026-09-01T10:30:00+00:00"

        updated = store.update_run(
            run.run_id, status="completed", completed_at=completed_at
        )
        loaded = store.get_run(run.run_id)

        assert updated.status == "completed"
        assert loaded.completed_at == completed_at
        assert loaded.started_at == run.started_at


def test_update_run_unknown_field_rejected(tmp_path):
    with make_store(tmp_path) as store:
        run = store.create_run(make_run())

        with pytest.raises(ValueError, match="tool_name"):
            store.update_run(run.run_id, tool_name="nope")


def test_update_missing_run_returns_none(tmp_path):
    with make_store(tmp_path) as store:
        assert store.update_run("run_unknown", status="failed") is None


def test_store_and_get_report(tmp_path):
    with make_store(tmp_path) as store:
        run = store.create_run(make_run(status="completed"))
        report = {
            "table": "prod.sales.orders",
            "overall_status": "needs_attention",
            "findings": [{"severity": "high", "evidence": ["R001"]}],
        }

        store.store_report(run.run_id, report)

        assert store.get_report(run.run_id) == report


def test_get_missing_report_returns_none(tmp_path):
    with make_store(tmp_path) as store:
        assert store.get_report("run_unknown") is None


def test_store_report_replaces_previous(tmp_path):
    with make_store(tmp_path) as store:
        run = store.create_run(make_run(status="completed"))
        store.store_report(run.run_id, {"overall_status": "healthy"})
        store.store_report(run.run_id, {"overall_status": "needs_attention"})

        assert store.get_report(run.run_id) == {"overall_status": "needs_attention"}


def test_store_report_accepts_pydantic_model(tmp_path):
    class Report(BaseModel):
        overall_status: str

    with make_store(tmp_path) as store:
        run = store.create_run(make_run(status="completed"))

        store.store_report(run.run_id, Report(overall_status="healthy"))

        assert store.get_report(run.run_id) == {"overall_status": "healthy"}


def test_persistence_survives_reopen(tmp_path):
    db_path = tmp_path / "sta.sqlite3"
    run = make_run(status="completed")
    with ResultStore(db_path) as store:
        store.create_run(run)
        store.store_report(run.run_id, {"overall_status": "healthy"})

    with ResultStore(db_path) as store:
        assert store.get_run(run.run_id) == run
        assert store.get_report(run.run_id) == {"overall_status": "healthy"}