"""Focused tests for the knowledge evidence-context guard
(Architecture.md #20, #35; observed failure run_03521df37c49: the model called
knowledge tools repeatedly and never run_query).

Contract: before the run's first stored measurement (R001+) exists,
``search_knowledge``/``read_knowledge`` return a typed safe precondition error
directing the Investigator to ``run_query`` first; R000 (the run-scoped full
schema) does not count as a measurement; a failed measurement attempt does not
unlock knowledge; after one stored measurement, knowledge is available
normally. The guard only changes when knowledge is served — it never
diagnoses, scores, or recommends.
"""

import pytest

from sta.context.table_context import TableContext
from sta.investigator.agent import InvestigationSession, InvestigationTools
from sta.knowledge import KnowledgeBase
from sta.results.models import QueryResult, RunRecord
from sta.results.store import FULL_SCHEMA_RESULT_ID, ResultStore

TABLE = "demo.sales.orders"
SNAPSHOT = "9182781280348117982"
RUN_ID = "run_guard_test"


class FailingRunner:
    """Runner seam that fails every query with a typed execution error."""

    def run(self, tool_name, parameters=None):
        from sta.execution.errors import BackendExecutionError

        raise BackendExecutionError(tool_name, "backend unavailable")


@pytest.fixture
def store(tmp_path):
    store = ResultStore(tmp_path / "results.sqlite3")
    store.create_run(RunRecord(run_id=RUN_ID, table=TABLE, snapshot_id=SNAPSHOT, status="running"))
    yield store
    store.close()


@pytest.fixture
def knowledge(tmp_path):
    root = tmp_path / "knowledge"
    (root / "runbooks").mkdir(parents=True)
    (root / "runbooks" / "file-sizing.md").write_text(
        "# File sizing\n\nPreferred data-file range: 256-512 MiB.\n",
        encoding="utf-8",
    )
    return KnowledgeBase(root)


def make_tools(store, knowledge, runner=None) -> InvestigationTools:
    session = InvestigationSession(
        run_id=RUN_ID,
        table=TABLE,
        snapshot_id=SNAPSHOT,
        table_context=TableContext(table=TABLE, schema_summary=[], column_groups={}),
        runner=runner,
        store=store,
        knowledge=knowledge,
    )
    return InvestigationTools(session)


def store_measurement(store: ResultStore) -> str:
    return store.store_result(
        QueryResult(
            run_id=RUN_ID,
            tool_name="get_file_layout",
            query_version="local:v1",
            table=TABLE,
            snapshot_id=SNAPSHOT,
            row_count=1,
            payload={"file_count": 3},
        )
    )


def assert_precondition_error(error: dict) -> None:
    assert error["error"] is True
    assert error["error_class"] == "no_measurement_evidence"
    assert error["retryable"] is True
    assert "run_query" in error["message"]
    assert "get_file_layout" in error["message"]


# ---------------------------------------------------------------------------
# Guard: before the first stored measurement
# ---------------------------------------------------------------------------


def test_search_knowledge_before_first_measurement_is_rejected(store, knowledge):
    store.store_full_schema(
        RUN_ID, {"ref": FULL_SCHEMA_RESULT_ID, "fields": []},
        tool_name="full_schema", query_version="startup_context:v1",
        table=TABLE, snapshot_id=SNAPSHOT, row_count=0,
    )
    tools = make_tools(store, knowledge)

    error = tools.search_knowledge("small files")

    assert_precondition_error(error)


def test_read_knowledge_before_first_measurement_is_rejected(store, knowledge):
    store.store_full_schema(
        RUN_ID, {"ref": FULL_SCHEMA_RESULT_ID, "fields": []},
        tool_name="full_schema", query_version="startup_context:v1",
        table=TABLE, snapshot_id=SNAPSHOT, row_count=0,
    )
    tools = make_tools(store, knowledge)

    error = tools.read_knowledge("runbooks/file-sizing.md")

    assert_precondition_error(error)


def test_guard_rejection_emits_no_event(store, knowledge):
    tools = make_tools(store, knowledge)

    tools.search_knowledge("small files")
    tools.read_knowledge("runbooks/file-sizing.md")

    assert store.list_events(RUN_ID) == []


def test_failed_measurement_attempt_does_not_unlock_knowledge(store, knowledge):
    tools = make_tools(store, knowledge, runner=FailingRunner())

    failed = tools.run_query("get_file_layout", {})
    assert failed["error"] is True

    assert_precondition_error(tools.search_knowledge("small files"))


def test_guard_is_per_run(store, knowledge):
    """Another run's stored measurements never unlock this run's knowledge."""
    store.create_run(RunRecord(run_id="run_other", table=TABLE, status="running"))
    other = store.store_result(
        QueryResult(run_id="run_other", tool_name="get_file_layout",
                    query_version="local:v1", table=TABLE, row_count=1)
    )
    assert other == "R001"
    tools = make_tools(store, knowledge)

    assert_precondition_error(tools.search_knowledge("small files"))


# ---------------------------------------------------------------------------
# After the first stored measurement: knowledge available normally
# ---------------------------------------------------------------------------


def test_search_knowledge_works_after_first_measurement(store, knowledge):
    store_measurement(store)
    tools = make_tools(store, knowledge)

    response = tools.search_knowledge("file sizing")

    assert "error" not in response
    assert any(hit["path"] == "runbooks/file-sizing.md" for hit in response["hits"])


def test_read_knowledge_works_after_first_measurement(store, knowledge):
    store_measurement(store)
    tools = make_tools(store, knowledge)

    document = tools.read_knowledge("runbooks/file-sizing.md")

    assert document["path"] == "runbooks/file-sizing.md"
    assert "256-512 MiB" in "\n".join(document["lines"])