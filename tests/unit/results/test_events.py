"""Progress event persistence tests
(Runtime_Environments_UI.md #25, #45)."""

import sqlite3

import pytest

from sta.results import ResultStore, RunRecord, new_run_id


def make_store(tmp_path) -> ResultStore:
    return ResultStore(tmp_path / "sta.sqlite3")


def make_run() -> RunRecord:
    return RunRecord(run_id=new_run_id(), table="prod.sales.orders")


def test_event_ids_are_monotonic_per_run(tmp_path):
    with make_store(tmp_path) as store:
        run_a = make_run()
        run_b = make_run()
        store.create_run(run_a)
        store.create_run(run_b)

        ev1 = store.append_event(run_a.run_id, "run_started")
        ev2 = store.append_event(run_a.run_id, "tool_requested", {"tool": "get_file_layout"})
        ev_b = store.append_event(run_b.run_id, "run_started")

        assert (ev1.event_id, ev2.event_id) == (1, 2)
        assert ev_b.event_id == 1


def test_appended_event_matches_contract_envelope(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        timestamp = "2026-09-01T10:30:12+00:00"

        event = store.append_event(
            run.run_id,
            "query_completed",
            {"tool": "get_file_layout", "duration_ms": 1260},
            timestamp=timestamp,
        )

        assert event.event_id == 1
        assert event.run_id == run.run_id
        assert event.type == "query_completed"
        assert event.timestamp == timestamp
        assert event.data == {"tool": "get_file_layout", "duration_ms": 1260}


def test_event_data_defaults_to_empty_dict(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)

        event = store.append_event(run.run_id, "run_started")

        assert event.data == {}


def test_list_events_returns_ordered_history(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        store.append_event(run.run_id, "run_started")
        store.append_event(run.run_id, "tool_requested", {"tool": "get_file_layout"})
        store.append_event(run.run_id, "result_stored", {"result_id": "R001", "row_count": 1})

        events = store.list_events(run.run_id)

        assert [e.event_id for e in events] == [1, 2, 3]
        assert [e.type for e in events] == [
            "run_started",
            "tool_requested",
            "result_stored",
        ]
        assert events[1].data == {"tool": "get_file_layout"}
        assert events[2].data == {"result_id": "R001", "row_count": 1}


def test_list_events_after_event_id_for_sse_replay(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        for event_type in ("run_started", "tool_requested", "query_started", "query_completed"):
            store.append_event(run.run_id, event_type)

        replayed = store.list_events(run.run_id, after_event_id=2)

        assert [e.event_id for e in replayed] == [3, 4]
        assert store.list_events(run.run_id, after_event_id=99) == []


def test_event_persistence_survives_reopen(tmp_path):
    db_path = tmp_path / "sta.sqlite3"
    run = make_run()
    with ResultStore(db_path) as store:
        store.create_run(run)
        store.append_event(run.run_id, "run_started")

    with ResultStore(db_path) as store:
        next_event = store.append_event(run.run_id, "table_resolved")
        assert next_event.event_id == 2
        assert [e.type for e in store.list_events(run.run_id)] == [
            "run_started",
            "table_resolved",
        ]


def test_event_requires_existing_run(tmp_path):
    with make_store(tmp_path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.append_event("run_unknown", "run_started")


def test_event_payload_is_persisted_as_safe_payload_json(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        store.append_event(run.run_id, "tool_requested", {"tool": "get_file_layout"})

        raw = sqlite3.connect(tmp_path / "sta.sqlite3").execute(
            "SELECT safe_payload_json FROM events"
        ).fetchone()[0]

        assert raw == '{"tool": "get_file_layout"}'