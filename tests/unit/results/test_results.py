"""Result store tests: monotonic per-run Rxxx ids and JSON payload round trips
(Architecture.md #17, #33)."""

import json
import sqlite3

import pytest

from sta.results import QueryResult, ResultStore, RunRecord, format_result_id, new_run_id


def make_store(tmp_path) -> ResultStore:
    return ResultStore(tmp_path / "sta.sqlite3")


def make_run() -> RunRecord:
    return RunRecord(run_id=new_run_id(), table="prod.sales.orders")


def make_result(run_id: str, **overrides) -> QueryResult:
    fields = {
        "run_id": run_id,
        "tool_name": "get_file_layout",
        "query_version": "file_layout:v3",
        "table": "prod.sales.orders",
        "parameters": {"snapshot": "current"},
        "schema": {"file_count": "int", "total_size_bytes": "int"},
        "row_count": 1,
        "payload": {
            "file_count": 18342,
            "total_size_bytes": 732344432,
            "median_file_size_bytes": 40894464,
        },
        "duration_ms": 1260,
    }
    fields.update(overrides)
    return QueryResult(**fields)


def test_result_ids_are_monotonic_per_run(tmp_path):
    with make_store(tmp_path) as store:
        run_a = make_run()
        run_b = make_run()
        store.create_run(run_a)
        store.create_run(run_b)

        ids_a = [store.store_result(make_result(run_a.run_id)) for _ in range(3)]
        ids_b = [store.store_result(make_result(run_b.run_id)) for _ in range(2)]

        assert ids_a == ["R001", "R002", "R003"]
        assert ids_b == ["R001", "R002"]


def test_result_id_allocation_survives_reopen(tmp_path):
    db_path = tmp_path / "sta.sqlite3"
    run = make_run()
    with ResultStore(db_path) as store:
        store.create_run(run)
        first = store.store_result(make_result(run.run_id))

    with ResultStore(db_path) as store:
        assert first == "R001"
        assert store.store_result(make_result(run.run_id)) == "R002"


def test_result_id_format_pads_and_stays_sortable(tmp_path):
    assert format_result_id(1) == "R001"
    assert format_result_id(999) == "R999"
    assert format_result_id(1000) == "R1000"


def test_store_result_sets_allocated_id_on_model(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        result = make_result(run.run_id, result_id="ignored")

        stored_id = store.store_result(result)

        assert stored_id == "R001"
        assert result.result_id == "R001"


def test_stored_results_are_immutable_append_only(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        first_id = store.store_result(make_result(run.run_id, payload={"file_count": 1}))

        # Storing the same measurements again appends a new result; the
        # existing one never changes.
        second_id = store.store_result(make_result(run.run_id, payload={"file_count": 2}))

        assert (first_id, second_id) == ("R001", "R002")
        assert store.get_result(run.run_id, "R001").payload == {"file_count": 1}
        assert store.get_result(run.run_id, "R002").payload == {"file_count": 2}


def test_result_round_trip_with_json_payload(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        result = make_result(
            run.run_id,
            snapshot_id="918278128",
            parameters={"snapshot": "918278128", "columns": ["order_date"]},
            payload={"rows": [{"file_count": 18342}], "nested": {"p90": 117.4}},
        )

        stored_id = store.store_result(result)
        loaded = store.get_result(run.run_id, stored_id)

        assert loaded == result


def test_result_without_payload_round_trips_as_none(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        result = make_result(run.run_id, payload=None, payload_location="results/run1/R001.json")

        stored_id = store.store_result(result)
        loaded = store.get_result(run.run_id, stored_id)

        assert loaded.payload is None
        assert loaded.payload_location == "results/run1/R001.json"


def test_list_results_ordered_by_result_id(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        for tool in ("get_snapshot_history", "get_file_layout", "get_delete_file_stats"):
            store.store_result(make_result(run.run_id, tool_name=tool))

        results = store.list_results(run.run_id)

        assert [r.result_id for r in results] == ["R001", "R002", "R003"]
        assert [r.tool_name for r in results] == [
            "get_snapshot_history",
            "get_file_layout",
            "get_delete_file_stats",
        ]


def test_list_results_empty_for_unknown_run(tmp_path):
    with make_store(tmp_path) as store:
        assert store.list_results("run_unknown") == []


def test_get_missing_result_returns_none(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        store.store_result(make_result(run.run_id))

        assert store.get_result(run.run_id, "R999") is None


def test_result_id_is_scoped_to_run(tmp_path):
    with make_store(tmp_path) as store:
        run_a = make_run()
        run_b = make_run()
        store.create_run(run_a)
        store.create_run(run_b)
        store.store_result(make_result(run_a.run_id, payload={"run": "a"}))

        assert store.get_result(run_b.run_id, "R001") is None
        assert store.get_result(run_a.run_id, "R001").payload == {"run": "a"}


def test_result_requires_existing_run(tmp_path):
    with make_store(tmp_path) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.store_result(make_result("run_unknown"))


def test_payload_is_stored_as_json(tmp_path):
    with make_store(tmp_path) as store:
        run = make_run()
        store.create_run(run)
        store.store_result(make_result(run.run_id, payload={"file_count": 18342}))

        raw = sqlite3.connect(tmp_path / "sta.sqlite3").execute(
            "SELECT payload_json FROM query_results"
        ).fetchone()[0]

        assert json.loads(raw) == {"file_count": 18342}