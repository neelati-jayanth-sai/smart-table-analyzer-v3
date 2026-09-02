"""E2E: the real Pydantic AI investigator loop over the FastAPI run lifecycle.

Closes the investigator-loop test hole: the integration suite drives only
``CallbackInvestigator``, so the actual Pydantic AI agent loop — model-issued
tool calls, output validation, model retry — was never exercised end-to-end.

These tests drive the REAL ``PydanticAiInvestigator`` (its deterministic tool
surface over the real ``QueryRunner`` + local Iceberg fixture, the real report
reference validator and the real run lifecycle) through the FastAPI app, with
the LLM replaced by a deterministic scripted model
(``ScriptedInvestigatorModel``). No second LLM is invoked, and nothing here
can act as a production fallback: the fake model exists only in ``tests/e2e``
and is injected explicitly as the investigator's model argument.

Scripted canonical conversation:

1. ``run_query(get_file_layout)``        -> R001 stored (first measurement),
2. ``read_result(R001)``                 -> stored measurement payload,
3. ``read_knowledge(runbooks/file-sizing.md)`` -> knowledge read after R001+,
4. structured final report               -> validated and persisted.

Asserted here: R001+ precedes knowledge (event order + precondition guard),
tool/event behavior, read-before-cite validation (model retry and safe
rejection), R000 metadata citations, report persistence, and that a malformed
result reference is safely rejected without failing the run.

Live-run regression scenarios (see docs/benchmark-expectations.md): the
temporal-candidate conversation measures column metadata before the targeted
scan and never before concluding insufficient evidence, and the delivered
system/user prompt carries the faithful-standards, compliance, metadata-first
and file-count materiality rules. No live model call is involved.
"""

import json
import re
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from sta.app.api import create_app
from sta.app.runs import RunComponents
from sta.config import Settings
from sta.context.table_metadata import StaticMetadataProvider, TableMetadata
from sta.execution.backends.local import LocalIcebergBackend
from sta.investigator.agent import INVESTIGATOR_TOOL_NAMES, PydanticAiInvestigator
from sta.knowledge.repository import KnowledgeBase
from sta.results.store import ResultStore
from tests.e2e.scripted_investigator_model import (
    InvestigatorScript,
    ScriptedInvestigatorModel,
    final_report_call,
    instruction_text,
    last_retry_prompts,
    last_tool_returns,
    tool_call,
    user_prompt_text,
)


TABLE = "demo.sales.orders"
SNAPSHOT_ID = "9182781280348117982"
MEASUREMENT_TOOL = "get_file_layout"
KNOWLEDGE_DOC = "runbooks/file-sizing.md"
EXISTING_BUT_UNREAD_DOC = "runbooks/compaction.md"
PARTITION_DOC = "runbooks/partitioning.md"
TEMPORAL_COLUMN = "created_at"
METRICS_TOOL = "get_column_metadata_metrics"
CANDIDATE_TOOL = "analyze_partition_candidate"

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


# ---------------------------------------------------------------------------
# harness: the DI app with the real Pydantic AI investigator + scripted model
# ---------------------------------------------------------------------------


def _build_settings(tmp_path: Path, knowledge_root: Path) -> Settings:
    return Settings(
        sta_env="local",
        db_path=str(tmp_path / "sta.sqlite3"),
        knowledge_path=str(knowledge_root),
        max_concurrent_runs=2,
        query_timeout_seconds=30,
    )


@pytest.fixture
def knowledge_corpus(tmp_path: Path) -> Path:
    """A minimal curated corpus: one document the script reads, one it only
    knows exists (for the read-before-cite scenarios)."""
    root = tmp_path / "knowledge"
    runbooks = root / "runbooks"
    runbooks.mkdir(parents=True)
    (runbooks / "file-sizing.md").write_text(
        "# File sizing\n\nKeep data files around 128 MB.\n"
    )
    (runbooks / "compaction.md").write_text(
        "# Compaction\n\nRewrite small files when they fall below guidance.\n"
    )
    (runbooks / "partitioning.md").write_text(
        "# Partition design\n\nPrefer temporal transforms on event timestamps; "
        "expect column metadata metrics before any targeted candidate analysis.\n"
    )
    return root


@pytest.fixture
def harness(tmp_path: Path, table_metadata: TableMetadata, local_table_fixture, knowledge_corpus: Path):
    """Build the dependency-injected app whose investigator is the REAL
    Pydantic AI agent with a test-supplied scripted model.

    Yields ``build(script) -> (app, components, models)``; ``models`` records
    every ``ScriptedInvestigatorModel`` built for the run so tests can assert
    on the exact conversation the agent produced.
    """
    built: list[RunComponents] = []

    def build(script: InvestigatorScript):
        settings = _build_settings(tmp_path, knowledge_corpus)
        models: list[ScriptedInvestigatorModel] = []

        def investigator_factory():
            model = ScriptedInvestigatorModel(script)
            models.append(model)
            return PydanticAiInvestigator(model)

        components = RunComponents(
            store=ResultStore(settings.db_path),
            knowledge=KnowledgeBase(settings.knowledge_path),
            metadata_provider=StaticMetadataProvider({TABLE: table_metadata}),
            backend_factory=lambda table, md: LocalIcebergBackend(local_table_fixture),
            investigator_factory=investigator_factory,
        )
        built.append(components)
        return create_app(components=components, settings=settings), components, models

    yield build
    for components in built:
        components.store.close()


def _create_run(client, *, table_name: str = TABLE) -> str:
    response = client.post("/api/runs", json={"table_name": table_name})
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


def _poll_until_terminal(client, run_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()
        if run["status"] in TERMINAL_STATUSES:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach a terminal state in time")


def _collect_sse_until_terminal(client, run_id: str, timeout: float = 10.0) -> list[dict[str, Any]]:
    """Stream the run's events over the real SSE endpoint until the terminal event."""
    events: list[dict[str, Any]] = []
    start = time.monotonic()
    with client.stream("GET", f"/api/runs/{run_id}/events") as response:
        assert response.status_code == 200
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, _, buffer = buffer.partition("\n\n")
                if not block.strip() or block.lstrip().startswith(":"):
                    continue
                event: dict[str, Any] = {}
                data_lines: list[str] = []
                for line in block.splitlines():
                    if line.startswith("id: "):
                        event["id"] = int(line[4:])
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                if data_lines:
                    event["data"] = json.loads("".join(data_lines))
                    events.append(event)
                    if event["data"].get("type") in {
                        "run_completed", "run_failed", "run_cancelled"
                    }:
                        return events
            if time.monotonic() - start > timeout:
                raise AssertionError("SSE stream did not reach the terminal event in time")
    return events


# ---------------------------------------------------------------------------
# scripted report payload and run identity (from the real user prompt)
# ---------------------------------------------------------------------------


def _identity_from_prompt(messages) -> tuple[str, str | None]:
    """Table identity as the model sees it: parsed from the real user prompt."""
    text = user_prompt_text(messages)
    table_match = re.search(r"^Table: (.+)$", text, re.MULTILINE)
    snapshot_match = re.search(r"^Pinned snapshot: (.+)$", text, re.MULTILINE)
    assert table_match, "the user prompt must carry the run's table identity"
    snapshot_id = None
    if snapshot_match and snapshot_match.group(1).strip() != "(not pinned)":
        snapshot_id = snapshot_match.group(1).strip()
    return table_match.group(1).strip(), snapshot_id


def _report_payload(table: str, snapshot_id: str | None, *, knowledge: list[str]) -> dict[str, Any]:
    """A schema-compliant final report: R001 measurement + R000 metadata citations."""
    return {
        "table": table,
        "snapshot_id": snapshot_id,
        "overall_status": "needs_attention",
        "current_issues": [
            {
                "finding": "Data files are far below the team's file-sizing guidance",
                "severity": "high",
                "confidence": "likely",
                "evidence": ["R001", "R000"],
                "knowledge": list(knowledge),
                "explanation": (
                    f"{MEASUREMENT_TOOL} (R001) measures files of ~10 MiB; the "
                    "file-sizing runbook prescribes ~128 MB files."
                ),
            }
        ],
        "immediate_remediation": [
            {
                "action": "Compact the current snapshot's small data files",
                "evidence": ["R001"],
                "knowledge": list(knowledge),
                "reason": (
                    "R001 shows the small measured files; the compaction runbook "
                    "describes rewriting them."
                ),
            }
        ],
        "future_table_design": {
            "partition_spec": {
                "current": "day(created_at), bucket[16](order_id)",
                "status": "no_change",
                "confidence": "verified",
                "evidence": ["R000"],
                "reasoning": "The current partition spec is startup metadata recorded in R000.",
            },
            "sort_order": {
                "current": "created_at asc nulls first",
                "status": "no_change",
                "confidence": "verified",
                "evidence": ["R000"],
                "reasoning": "The current sort order is startup metadata recorded in R000.",
            },
            "table_properties": [],
        },
        "no_change_decisions": [],
        "limitations": ["Workload/query-pattern analysis is disabled for this run."],
    }


# ---------------------------------------------------------------------------
# scenario scripts
# ---------------------------------------------------------------------------


def _measurement_first_script(observations: list[tuple[str, Any]]) -> InvestigatorScript:
    """The canonical scripted investigation: measurement -> read -> knowledge
    -> final report. Also records the real tool surface from the agent."""

    def script(messages, model_request_parameters):
        retries = last_retry_prompts(messages)
        assert not retries, f"unexpected model-retry prompt: {retries}"
        returns = last_tool_returns(messages)
        if not returns:
            observations.append(
                (
                    "tool_surface",
                    {
                        "tools": sorted(tool.name for tool in model_request_parameters.function_tools),
                        "output_tool": model_request_parameters.output_tools[0].name,
                    },
                )
            )
            return tool_call("run_query", {"tool_name": MEASUREMENT_TOOL, "parameters": {}})
        assert len(returns) == 1, "the script emits exactly one tool call per step"
        tool_name, content = returns[0].tool_name, returns[0].content
        observations.append((tool_name, content))
        if tool_name == "run_query":
            return tool_call("read_result", {"result_id": content["result_id"]})
        if tool_name == "read_result":
            return tool_call("read_knowledge", {"path": KNOWLEDGE_DOC})
        if tool_name == "read_knowledge":
            table, snapshot_id = _identity_from_prompt(messages)
            return final_report_call(
                model_request_parameters,
                _report_payload(table, snapshot_id, knowledge=[KNOWLEDGE_DOC]),
            )
        raise AssertionError(f"unexpected tool return in scripted run: {tool_name}")

    return script


def _knowledge_first_script(observations: list[tuple[str, Any]]) -> InvestigatorScript:
    """Probes read_knowledge before any measurement, records the typed
    precondition error, then performs the canonical sequence."""

    def script(messages, model_request_parameters):
        retries = last_retry_prompts(messages)
        assert not retries, f"unexpected model-retry prompt: {retries}"
        returns = last_tool_returns(messages)
        if not returns:
            observations.append(("knowledge_probe", None))
            return tool_call("read_knowledge", {"path": KNOWLEDGE_DOC})
        tool_name, content = returns[0].tool_name, returns[0].content
        observations.append((tool_name, content))
        if tool_name == "read_knowledge" and isinstance(content, dict) and content.get("error"):
            # precondition guard fired; measure first as instructed
            return tool_call("run_query", {"tool_name": MEASUREMENT_TOOL, "parameters": {}})
        if tool_name == "run_query":
            return tool_call("read_result", {"result_id": content["result_id"]})
        if tool_name == "read_result":
            return tool_call("read_knowledge", {"path": KNOWLEDGE_DOC})
        if tool_name == "read_knowledge":
            table, snapshot_id = _identity_from_prompt(messages)
            return final_report_call(
                model_request_parameters,
                _report_payload(table, snapshot_id, knowledge=[KNOWLEDGE_DOC]),
            )
        raise AssertionError(f"unexpected tool return in scripted run: {tool_name}")

    return script


def _malformed_result_script(observations: list[tuple[str, Any]]) -> InvestigatorScript:
    """Probes read_result with a malformed Rxxx id, then reads R001 properly."""

    def script(messages, model_request_parameters):
        retries = last_retry_prompts(messages)
        assert not retries, f"unexpected model-retry prompt: {retries}"
        returns = last_tool_returns(messages)
        if not returns:
            observations.append(("run_query_request", None))
            return tool_call("run_query", {"tool_name": MEASUREMENT_TOOL, "parameters": {}})
        tool_name, content = returns[0].tool_name, returns[0].content
        observations.append((tool_name, content))
        if tool_name == "run_query":
            return tool_call("read_result", {"result_id": "R12"})  # malformed: too few digits
        if tool_name == "read_result" and content.get("error"):
            # the malformed probe was safely rejected; read the real measurement
            return tool_call("read_result", {"result_id": "R001"})
        if tool_name == "read_result":
            return tool_call("read_knowledge", {"path": KNOWLEDGE_DOC})
        if tool_name == "read_knowledge":
            table, snapshot_id = _identity_from_prompt(messages)
            return final_report_call(
                model_request_parameters,
                _report_payload(table, snapshot_id, knowledge=[KNOWLEDGE_DOC]),
            )
        raise AssertionError(f"unexpected tool return in scripted run: {tool_name}")

    return script


def _unread_citation_script(
    observations: list[tuple[str, Any]], *, always_invalid: bool
) -> InvestigatorScript:
    """Cites an existing-but-unread knowledge doc in the final report.

    With ``always_invalid=False`` the script recovers after STA's model-retry
    feedback and submits the corrected report; with ``True`` it never corrects,
    so the real agent exhausts its output-retry budget and the run fails.
    """

    def script(messages, model_request_parameters):
        retries = last_retry_prompts(messages)
        if retries:
            observations.append(("output_retry", str(retries[0].content)))
            if always_invalid:
                knowledge = [EXISTING_BUT_UNREAD_DOC]
            else:
                knowledge = [KNOWLEDGE_DOC]
            table, snapshot_id = _identity_from_prompt(messages)
            return final_report_call(
                model_request_parameters,
                _report_payload(table, snapshot_id, knowledge=knowledge),
            )
        returns = last_tool_returns(messages)
        if not returns:
            return tool_call("run_query", {"tool_name": MEASUREMENT_TOOL, "parameters": {}})
        tool_name, content = returns[0].tool_name, returns[0].content
        observations.append((tool_name, content))
        if tool_name == "run_query":
            return tool_call("read_knowledge", {"path": KNOWLEDGE_DOC})
        if tool_name == "read_knowledge":
            # cite a document that exists but was never read -> validator ModelRetry
            return final_report_call(
                model_request_parameters,
                _report_payload(
                    *_identity_from_prompt(messages), knowledge=[EXISTING_BUT_UNREAD_DOC]
                ),
            )
        raise AssertionError(f"unexpected tool return in scripted run: {tool_name}")

    return script


def _temporal_candidate_script(
    observations: list[tuple[str, Any]], *, run_targeted: bool
) -> InvestigatorScript:
    """Live-run regression (events_partition_candidate): a partition
    recommendation is materially considered for a non-identifier temporal
    column, so the script measures the column's metadata metrics first and
    runs the targeted candidate analysis only when that metadata is
    insufficient (``run_targeted=False`` models the sufficient case, where the
    expensive tool must never run). Also records the prompts the model
    actually received on its first request."""
    stored: dict[str, str] = {}  # inner measurement tool -> Rxxx

    def script(messages, model_request_parameters):
        retries = last_retry_prompts(messages)
        assert not retries, f"unexpected model-retry prompt: {retries}"
        returns = last_tool_returns(messages)
        if not returns:
            observations.append(
                (
                    "prompt_surface",
                    {
                        "system": instruction_text(model_request_parameters),
                        "user": user_prompt_text(messages),
                    },
                )
            )
            return tool_call("run_query", {"tool_name": MEASUREMENT_TOOL, "parameters": {}})
        tool_name, content = returns[0].tool_name, returns[0].content
        observations.append((tool_name, content))
        if tool_name == "run_query":
            stored[content["tool_name"]] = content["result_id"]
            if content["tool_name"] == MEASUREMENT_TOOL:
                # metadata first: the temporal column under partition
                # consideration is measured with the cheap metadata tool
                return tool_call(
                    "run_query",
                    {"tool_name": METRICS_TOOL, "parameters": {"column": TEMPORAL_COLUMN}},
                )
            if content["tool_name"] == METRICS_TOOL and run_targeted:
                # metadata carried counts/bounds but not the distinct-value
                # evidence needed to judge the candidate -> targeted scan now
                return tool_call(
                    "run_query",
                    {"tool_name": CANDIDATE_TOOL, "parameters": {"column": TEMPORAL_COLUMN}},
                )
            return tool_call("read_knowledge", {"path": PARTITION_DOC})
        if tool_name == "read_knowledge":
            table, snapshot_id = _identity_from_prompt(messages)
            targeted = [stored[CANDIDATE_TOOL]] if CANDIDATE_TOOL in stored else []
            return final_report_call(
                model_request_parameters,
                _temporal_candidate_report(
                    table,
                    snapshot_id,
                    knowledge=[PARTITION_DOC],
                    metadata_results=[stored[METRICS_TOOL]],
                    targeted_results=targeted,
                ),
            )
        raise AssertionError(f"unexpected tool return in scripted run: {tool_name}")

    return script


def _temporal_candidate_report(
    table: str,
    snapshot_id: str | None,
    *,
    knowledge: list[str],
    metadata_results: list[str],
    targeted_results: list[str],
) -> dict[str, Any]:
    """Schema-compliant report for the temporal-candidate regression: R000
    metadata citation plus the metadata-metrics measurement (and the targeted
    result only when it was actually run)."""
    ran_targeted = bool(targeted_results)
    return {
        "table": table,
        "snapshot_id": snapshot_id,
        "overall_status": "healthy",
        "current_issues": [],
        "immediate_remediation": [],
        "future_table_design": {
            "partition_spec": {
                "current": "day(created_at), bucket[16](order_id)",
                "recommendation": "months(created_at)" if ran_targeted else None,
                "status": "consider" if ran_targeted else "no_change",
                "confidence": "likely",
                "evidence": ["R000", *metadata_results, *targeted_results],
                "knowledge": list(knowledge),
                "reasoning": (
                    "get_column_metadata_metrics carried value/null counts and bounds but "
                    "not distinct values, so the targeted analyze_partition_candidate ran "
                    "to judge the candidate."
                    if ran_targeted
                    else "get_column_metadata_metrics alone was sufficient to judge the "
                    "temporal candidate; the targeted scan was unnecessary."
                ),
                "caveats": [],
            },
            "sort_order": {
                "current": "created_at asc nulls first",
                "status": "no_change",
                "confidence": "verified",
                "evidence": ["R000"],
                "reasoning": "The current sort order is startup metadata recorded in R000.",
            },
            "table_properties": [],
        },
        "no_change_decisions": [],
        "limitations": ["Workload/query-pattern analysis is disabled for this run."],
    }


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


def test_scripted_investigator_completes_full_run_lifecycle(harness):
    """The canonical scripted conversation drives the REAL agent loop end to
    end: R000+R001 stored, knowledge read after R001+, report validated
    (R000 metadata citations, read-before-cite) and persisted."""
    observations: list[tuple[str, Any]] = []
    app, components, models = harness(_measurement_first_script(observations))
    with TestClient(app) as client:
        run_id = _create_run(client)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "completed"
        assert final["error"] is None
        assert final["snapshot_id"] == SNAPSHOT_ID
        assert final["table"] == TABLE

        # the scripted conversation happened through the real agent, in order
        assert [label for label, _ in observations] == [
            "tool_surface", "run_query", "read_result", "read_knowledge",
        ]
        assert len(models[0].requests) == 4

        surface = dict(observations[0][1])
        assert surface["tools"] == sorted(INVESTIGATOR_TOOL_NAMES)
        assert surface["output_tool"]

        run_query_return = observations[1][1]
        assert run_query_return["result_id"] == "R001"
        assert run_query_return["tool_name"] == MEASUREMENT_TOOL
        assert run_query_return["row_count"] == 1
        assert run_query_return["payload"]["file_count"] == 3  # real local fixture measurement

        read_return = observations[2][1]
        assert read_return["result_id"] == "R001"
        assert read_return["payload"]["file_count"] == 3
        # aggregate payloads pass through pagination untouched
        assert read_return["total_rows"] is None
        assert read_return["truncated"] is False

        knowledge_return = observations[3][1]
        assert knowledge_return["path"] == KNOWLEDGE_DOC
        assert knowledge_return["lines"]

        # stored results through the API
        results = client.get(f"/api/runs/{run_id}/results").json()["results"]
        assert [r["result_id"] for r in results] == ["R000", "R001"]
        assert results[0]["tool_name"] == "full_schema"
        assert results[1]["tool_name"] == MEASUREMENT_TOOL

        # tool/event behavior: R001 stored strictly before the knowledge read
        events = components.store.list_events(run_id)
        types = [event.type for event in events]
        assert "investigator_started" in types
        assert "tool_requested" in types
        assert "query_started" in types
        requested = [event for event in events if event.type == "tool_requested"]
        assert any(
            event.data.get("tool") == MEASUREMENT_TOOL
            and event.data.get("parameters") == {}
            for event in requested
        )
        r001_stored = [
            event for event in events
            if event.type == "result_stored" and event.data.get("result_id") == "R001"
        ]
        knowledge_reads = [event for event in events if event.type == "knowledge_read"]
        assert len(r001_stored) == 1
        assert len(knowledge_reads) == 1
        assert knowledge_reads[0].data == {"path": KNOWLEDGE_DOC}
        assert r001_stored[0].event_id < knowledge_reads[0].event_id

        # the same events stream over the real SSE endpoint, ending terminal
        sse = _collect_sse_until_terminal(client, run_id)
        sse_types = [entry["data"]["type"] for entry in sse]
        assert sse_types[-1] == "run_completed"
        assert "result_stored" in sse_types
        assert "knowledge_read" in sse_types
        assert "report_ready" in sse_types

        # the validated report is persisted and retrievable
        report_response = client.get(f"/api/runs/{run_id}/report")
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["table"] == TABLE
        assert report["snapshot_id"] == SNAPSHOT_ID
        assert report["overall_status"] == "needs_attention"
        issue = report["current_issues"][0]
        assert issue["evidence"] == ["R001", "R000"]
        assert issue["knowledge"] == [KNOWLEDGE_DOC]
        # R000 metadata citations in both design sections (enforced by the
        # real validator; asserted explicitly on the stored report)
        assert "R000" in report["future_table_design"]["partition_spec"]["evidence"]
        assert "R000" in report["future_table_design"]["sort_order"]["evidence"]
        assert report["future_table_design"]["partition_spec"]["status"] == "no_change"


def test_knowledge_stays_unavailable_until_first_measurement(harness):
    """The knowledge precondition guard is real: a scripted read_knowledge
    before any measurement gets the typed safe error, and only after R001 is
    the document actually served and its read event persisted."""
    observations: list[tuple[str, Any]] = []
    app, components, _models = harness(_knowledge_first_script(observations))
    with TestClient(app) as client:
        run_id = _create_run(client)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "completed"

        labels = [label for label, _ in observations]
        assert labels == [
            "knowledge_probe", "read_knowledge", "run_query", "read_result", "read_knowledge",
        ]

        precondition = observations[1][1]
        assert precondition["error"] is True
        assert precondition["error_class"] == "no_measurement_evidence"
        assert precondition["retryable"] is True
        assert "run_query" in precondition["message"]

        successful_read = observations[4][1]
        assert successful_read["path"] == KNOWLEDGE_DOC

        # exactly one knowledge_read event (the rejected probe emitted none),
        # and it follows the R001 storage
        events = components.store.list_events(run_id)
        knowledge_reads = [event for event in events if event.type == "knowledge_read"]
        assert len(knowledge_reads) == 1
        r001_stored = [
            event for event in events
            if event.type == "result_stored" and event.data.get("result_id") == "R001"
        ]
        assert len(r001_stored) == 1
        assert r001_stored[0].event_id < knowledge_reads[0].event_id

        # the report citing the (actually read) knowledge doc is persisted
        assert client.get(f"/api/runs/{run_id}/report").status_code == 200


def test_malformed_result_reference_is_safely_rejected(harness):
    """A malformed Rxxx read_result call returns the typed rejection to the
    model and does not fail the run; the scripted conversation recovers and
    the validated report persists."""
    observations: list[tuple[str, Any]] = []
    app, components, _models = harness(_malformed_result_script(observations))
    with TestClient(app) as client:
        run_id = _create_run(client)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "completed"
        assert final["error"] is None

        labels = [label for label, _ in observations]
        assert labels == [
            "run_query_request", "run_query", "read_result", "read_result", "read_knowledge",
        ]

        malformed = observations[2][1]
        assert malformed["error"] is True
        assert malformed["error_class"] == "invalid_result_reference"
        assert "malformed" in malformed["message"]

        recovered = observations[3][1]
        assert recovered["result_id"] == "R001"

        # the run still produced R000+R001 and a valid persisted report
        results = client.get(f"/api/runs/{run_id}/results").json()["results"]
        assert [r["result_id"] for r in results] == ["R000", "R001"]
        report_response = client.get(f"/api/runs/{run_id}/report")
        assert report_response.status_code == 200
        assert report_response.json()["current_issues"][0]["evidence"] == ["R001", "R000"]


def test_citing_unread_knowledge_is_rejected_then_recovered_by_model_retry(harness):
    """The real output validator rejects a report citing an unread knowledge
    document via a model-retry prompt; the scripted model recovers and the
    corrected report persists."""
    observations: list[tuple[str, Any]] = []
    app, components, models = harness(
        _unread_citation_script(observations, always_invalid=False)
    )
    with TestClient(app) as client:
        run_id = _create_run(client)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "completed"
        assert final["error"] is None

        labels = [label for label, _ in observations]
        assert labels == ["run_query", "read_knowledge", "output_retry"]

        retry_message = observations[2][1]
        assert EXISTING_BUT_UNREAD_DOC in retry_message
        assert "never read" in retry_message
        # the retry feedback names STA's deterministic rule, not raw output
        assert "read_knowledge" in retry_message

        # one model retry through the real agent: 4 requests total
        assert len(models[0].requests) == 4

        # the corrected report persists and cites only the actually-read doc
        report_response = client.get(f"/api/runs/{run_id}/report")
        assert report_response.status_code == 200
        report = report_response.json()
        assert report["current_issues"][0]["knowledge"] == [KNOWLEDGE_DOC]
        events = components.store.list_events(run_id)
        assert [
            event.data.get("path")
            for event in events
            if event.type == "knowledge_read"
        ] == [KNOWLEDGE_DOC]


def test_always_unread_citation_fails_run_with_safe_rejection_event(harness):
    """If the model never corrects the unread citation, the real agent
    exhausts its output-retry budget, the run fails safely with a typed
    report_rejected event, and no report is persisted."""
    observations: list[tuple[str, Any]] = []
    app, components, models = harness(
        _unread_citation_script(observations, always_invalid=True)
    )
    with TestClient(app) as client:
        run_id = _create_run(client)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "failed"
        assert final["error"] is not None
        # the sanitized UnexpectedModelBehavior message (no raw model output)
        assert "conform to the report contract" in final["error"].lower()

        # the script was asked twice to correct itself before rejection
        assert [label for label, _ in observations].count("output_retry") == 2
        assert len(models[0].requests) == 5

        # no report is persisted
        assert client.get(f"/api/runs/{run_id}/report").status_code == 404

        # the event stream carries the safe rejection details, not raw output
        events = components.store.list_events(run_id)
        types = [event.type for event in events]
        assert "report_rejected" in types
        assert types[-1] == "run_failed"
        rejected = [event for event in events if event.type == "report_rejected"][0]
        assert rejected.data["error_class"] == "report_rejected"
        assert rejected.data["validation_summary"]["reason"] == "unexpected_model_behavior"
        event_text = json.dumps(rejected.data)
        assert "raw_output" not in event_text
        assert KNOWLEDGE_DOC not in event_text  # no model/report content leaks
        failed = [event for event in events if event.type == "run_failed"][-1]
        assert failed.data["error_class"] == "report_rejected"
        assert failed.data["details"]["reason"] == "unexpected_model_behavior"

        # evidence collected before the failure is preserved
        results = client.get(f"/api/runs/{run_id}/results").json()["results"]
        assert [r["result_id"] for r in results] == ["R000", "R001"]

# ---------------------------------------------------------------------------
# live-run regressions: temporal-candidate tool order and prompt rules
# ---------------------------------------------------------------------------


def _assert_prompt_carries_report_rules(surface: dict[str, str]) -> None:
    """The model actually received the corrective rules (system instructions
    via instruction parts, measurement tool catalog in the user prompt)."""
    system, user = surface["system"], surface["user"]
    # faithful standards + compliance judged against the exact read range
    assert "exactly as written" in system
    assert "compliant only when it falls inside the range" in system
    assert "outside that range is a deviation to report" in system
    # temporal-candidate discipline: metadata metrics before targeted analysis
    assert "get_column_metadata_metrics" in system
    assert "analyze_partition_candidate" in system
    assert "metadata measurement is insufficient" in system
    # small absolute file count is not material fragmentation
    assert "small absolute file count" in system
    assert "material fragmentation" in system
    assert "materiality criteria" in system
    # the compact user prompt carries both tools in the catalog
    assert METRICS_TOOL in user
    assert CANDIDATE_TOOL in user


def test_temporal_candidate_measures_metadata_before_targeted_analysis(harness):
    """Regression for the events_partition_candidate live run: with a
    partition recommendation materially considered for a temporal column, the
    scripted conversation measures get_column_metadata_metrics FIRST and runs
    analyze_partition_candidate only afterwards (metadata insufficient), and
    the run persists a report citing both in order."""
    observations: list[tuple[str, Any]] = []
    app, components, models = harness(_temporal_candidate_script(observations, run_targeted=True))
    with TestClient(app) as client:
        run_id = _create_run(client)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "completed"
        assert final["error"] is None

        # exact tool-selection order: measurement -> metadata metrics ->
        # targeted candidate -> knowledge read -> report
        labels = [label for label, _ in observations]
        assert labels == [
            "prompt_surface", "run_query", "run_query", "run_query", "read_knowledge",
        ]

        _assert_prompt_carries_report_rules(observations[0][1])

        file_layout = observations[1][1]
        assert file_layout["result_id"] == "R001"
        metrics = observations[2][1]
        assert metrics["result_id"] == "R002"
        assert metrics["tool_name"] == METRICS_TOOL
        assert metrics["payload"]["files_measured"] == 3
        assert metrics["payload"]["value_count_sum"] == 300
        targeted = observations[3][1]
        assert targeted["result_id"] == "R003"
        assert targeted["tool_name"] == CANDIDATE_TOOL
        assert targeted["payload"]["distinct_count"] == 2

        # stored results in the metadata-first order the rules prescribe
        results = client.get(f"/api/runs/{run_id}/results").json()["results"]
        assert [r["result_id"] for r in results] == ["R000", "R001", "R002", "R003"]
        assert [r["tool_name"] for r in results] == [
            "full_schema", MEASUREMENT_TOOL, METRICS_TOOL, CANDIDATE_TOOL,
        ]

        # event order proves the sequence: each measurement stored strictly
        # before the next tool request, targeted strictly after the metrics
        events = components.store.list_events(run_id)
        requested = [
            (event.event_id, event.data.get("tool"), event.data.get("parameters"))
            for event in events
            if event.type == "tool_requested"
        ]
        assert [
            (tool, parameters)
            for _, tool, parameters in requested
        ] == [
            (MEASUREMENT_TOOL, {}),
            (METRICS_TOOL, {"column": TEMPORAL_COLUMN}),
            (CANDIDATE_TOOL, {"column": TEMPORAL_COLUMN}),
        ]
        metrics_requested_id = requested[1][0]
        targeted_requested = requested[2][0]
        assert metrics_requested_id < targeted_requested
        stored_events = {
            event.data.get("result_id"): event.event_id
            for event in events
            if event.type == "result_stored"
        }
        assert stored_events["R001"] < stored_events["R002"] < stored_events["R003"]
        knowledge_reads = [event for event in events if event.type == "knowledge_read"]
        assert [event.data.get("path") for event in knowledge_reads] == [PARTITION_DOC]
        assert stored_events["R003"] < knowledge_reads[0].event_id

        # the validated report persists and cites the targeted result
        report = client.get(f"/api/runs/{run_id}/report").json()
        partition_spec = report["future_table_design"]["partition_spec"]
        assert partition_spec["status"] == "consider"
        assert partition_spec["evidence"] == ["R000", "R002", "R003"]
        assert partition_spec["knowledge"] == [PARTITION_DOC]
        assert len(models[0].requests) == 5


def test_temporal_candidate_metadata_sufficient_skips_targeted_scan(harness):
    """The cheap-metadata-first rule's other half: when the metadata metrics
    are sufficient to judge the temporal candidate, the expensive targeted
    scan must never run — and the report still cites the metadata result."""
    observations: list[tuple[str, Any]] = []
    app, components, models = harness(_temporal_candidate_script(observations, run_targeted=False))
    with TestClient(app) as client:
        run_id = _create_run(client)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "completed"
        assert final["error"] is None

        labels = [label for label, _ in observations]
        assert labels == ["prompt_surface", "run_query", "run_query", "read_knowledge"]

        metrics = observations[2][1]
        assert metrics["result_id"] == "R002"
        assert metrics["tool_name"] == METRICS_TOOL

        # no targeted candidate analysis happened anywhere in the run
        events = components.store.list_events(run_id)
        assert not any(
            event.type == "tool_requested" and event.data.get("tool") == CANDIDATE_TOOL
            for event in events
        )
        stored_ids = [r["result_id"] for r in client.get(f"/api/runs/{run_id}/results").json()["results"]]
        assert stored_ids == ["R000", "R001", "R002"]

        # the validated report cites the metadata measurement, not a scan
        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["future_table_design"]["partition_spec"]["status"] == "no_change"
        assert report["future_table_design"]["partition_spec"]["evidence"] == ["R000", "R002"]
        assert report["future_table_design"]["partition_spec"]["recommendation"] is None
        assert len(models[0].requests) == 4


# ---------------------------------------------------------------------------
# live-run regression: evaluate one candidate column against a bad current spec
# ---------------------------------------------------------------------------


STATUS_TABLE = "local.demo.events_status_partitioned"
STATUS_CANDIDATE = "event_timestamp"


@pytest.fixture
def status_harness(
    tmp_path: Path, status_table_metadata: TableMetadata, status_partitioned_fixture, knowledge_corpus: Path
):
    """Harness builder for the bad status-partitioned fixture.

    Yields ``build(script) -> (app, components, models)``.
    """
    built: list[RunComponents] = []

    def build(script: InvestigatorScript):
        settings = _build_settings(tmp_path, knowledge_corpus)
        models: list[ScriptedInvestigatorModel] = []

        def investigator_factory():
            model = ScriptedInvestigatorModel(script)
            models.append(model)
            return PydanticAiInvestigator(model)

        components = RunComponents(
            store=ResultStore(settings.db_path),
            knowledge=KnowledgeBase(settings.knowledge_path),
            metadata_provider=StaticMetadataProvider(
                {STATUS_TABLE: status_table_metadata}
            ),
            backend_factory=lambda table, md: LocalIcebergBackend(status_partitioned_fixture),
            investigator_factory=investigator_factory,
        )
        built.append(components)
        return create_app(components=components, settings=settings), components, models

    yield build
    for components in built:
        components.store.close()


def _status_candidate_script(observations: list[tuple[str, Any]]) -> InvestigatorScript:
    """Scripted evaluation of a temporal candidate against a bad identity(status)
    current partition. Demonstrates the evidence workflow: file layout -> current
    spec facts -> column metadata -> targeted candidate distribution -> knowledge
    -> report. Never scans all columns."""

    def script(messages, model_request_parameters):
        retries = last_retry_prompts(messages)
        assert not retries, f"unexpected model-retry prompt: {retries}"
        returns = last_tool_returns(messages)
        if not returns:
            observations.append(
                (
                    "prompt_surface",
                    {
                        "system": instruction_text(model_request_parameters),
                        "user": user_prompt_text(messages),
                    },
                )
            )
            return tool_call("run_query", {"tool_name": "get_file_layout", "parameters": {}})
        tool_name, content = returns[0].tool_name, returns[0].content
        observations.append((tool_name, content))
        if tool_name == "run_query":
            if content["tool_name"] == "get_file_layout":
                return tool_call(
                    "run_query", {"tool_name": "get_partition_layout", "parameters": {}}
                )
            if content["tool_name"] == "get_partition_layout":
                # metadata first; bounds are deliberately incomplete in the fixture
                return tool_call(
                    "run_query",
                    {
                        "tool_name": "get_column_metadata_metrics",
                        "parameters": {"column": STATUS_CANDIDATE},
                    },
                )
            if content["tool_name"] == "get_column_metadata_metrics":
                # metadata is insufficient (only 4 of 12 files have bounds)
                return tool_call(
                    "run_query",
                    {
                        "tool_name": "analyze_partition_candidate",
                        "parameters": {"column": STATUS_CANDIDATE},
                    },
                )
            if content["tool_name"] == "analyze_partition_candidate":
                return tool_call("read_knowledge", {"path": PARTITION_DOC})
        if tool_name == "read_knowledge":
            table, snapshot_id = _identity_from_prompt(messages)
            return final_report_call(
                model_request_parameters,
                {
                    "table": table,
                    "snapshot_id": snapshot_id,
                    "overall_status": "needs_attention",
                    "current_issues": [
                        {
                            "finding": (
                                "Current partition by status creates a few very large "
                                "partitions with no time-pruning boundary"
                            ),
                            "severity": "high",
                            "confidence": "likely",
                            "evidence": ["R001", "R002", "R004"],
                            "knowledge": [PARTITION_DOC],
                            "explanation": (
                                "get_partition_layout (R002) shows 4 status partitions with "
                                "3 files/300 records each. analyze_partition_candidate (R004) "
                                "shows days(event_timestamp) would create 3 time-bounded "
                                "partitions with 4 files/400 records each."
                            ),
                            "likely_cause": "status was chosen as a partition key without considering query-time pruning patterns",
                        }
                    ],
                    "immediate_remediation": [],
                    "future_table_design": {
                        "partition_spec": {
                            "current": "status",
                            "recommendation": "days(event_timestamp)",
                            "status": "consider",
                            "confidence": "likely",
                            "evidence": ["R000", "R001", "R002", "R003", "R004"],
                            "knowledge": [PARTITION_DOC],
                            "reasoning": (
                                "R002 measures the current identity(status) spec: only 4 "
                                "partitions, each mixing 3 days of data. R003 metadata and R004 "
                                "targeted distribution show event_timestamp spans 3 days and a "
                                "day transform would create 3 time-bounded partitions with 4 "
                                "files/400 records each. The candidate is non-identifier and "
                                "its distribution facts are measured; it merits consideration "
                                "as a future spec. Existing data would need a rewrite to realize "
                                "the new layout for historical files."
                            ),
                            "caveats": [
                                "Only event_timestamp was analyzed; no other candidate column was profiled.",
                                "Workload/query-pattern evidence is disabled, so query-pruning benefit is assumed from the column semantics.",
                            ],
                        },
                        "sort_order": {
                            "current": "none",
                            "status": "no_change",
                            "confidence": "verified",
                            "evidence": ["R000"],
                            "reasoning": "The current sort order is startup metadata recorded in R000.",
                        },
                        "table_properties": [],
                    },
                    "no_change_decisions": [],
                    "limitations": [
                        "Workload/query-pattern analysis is disabled for this run.",
                        "Only one partition candidate (event_timestamp) was analyzed; other columns were not profiled.",
                    ],
                },
            )
        raise AssertionError(f"unexpected tool return in scripted run: {tool_name}")

    return script


def test_status_partitioned_candidate_evaluation(status_harness):
    """The agent evaluates a proposed non-identifier temporal column against the
    current bad spec using only targeted measurements, never scanning all
    columns."""
    observations: list[tuple[str, Any]] = []
    app, components, models = status_harness(_status_candidate_script(observations))
    with TestClient(app) as client:
        run_id = _create_run(client, table_name=STATUS_TABLE)

        final = _poll_until_terminal(client, run_id)
        assert final["status"] == "completed"
        assert final["error"] is None
        assert final["table"] == STATUS_TABLE

        labels = [label for label, _ in observations]
        assert labels == [
            "prompt_surface",
            "run_query",
            "run_query",
            "run_query",
            "run_query",
            "read_knowledge",
        ]

        _assert_prompt_carries_report_rules(observations[0][1])
        _assert_prompt_carries_candidate_workflow(observations[0][1])

        # tool order: file layout -> partition layout -> metadata -> targeted scan
        events = components.store.list_events(run_id)
        requested = [
            (event.event_id, event.data.get("tool"), event.data.get("parameters"))
            for event in events
            if event.type == "tool_requested"
        ]
        assert [tool for _, tool, _ in requested] == [
            "get_file_layout",
            "get_partition_layout",
            "get_column_metadata_metrics",
            "analyze_partition_candidate",
        ]
        assert requested[2][2] == {"column": STATUS_CANDIDATE}
        assert requested[3][2] == {"column": STATUS_CANDIDATE}

        # no broad profiling: candidate analysis was called exactly once
        candidate_requests = [
            event for event in events
            if event.type == "tool_requested"
            and event.data.get("tool") == "analyze_partition_candidate"
        ]
        assert len(candidate_requests) == 1
        assert candidate_requests[0].data["parameters"]["column"] == STATUS_CANDIDATE

        stored = {
            event.data.get("result_id"): event.event_id
            for event in events
            if event.type == "result_stored"
        }
        assert list(stored.keys()) == ["R000", "R001", "R002", "R003", "R004"]
        assert stored["R002"] < stored["R003"] < stored["R004"]

        # the targeted scan carried distribution facts
        candidate_response = client.get(f"/api/runs/{run_id}/results/R004")
        assert candidate_response.status_code == 200
        candidate_result = candidate_response.json()
        assert candidate_result["tool_name"] == "analyze_partition_candidate"
        payload = candidate_result["payload"]
        assert payload["distinct_count"] == 3
        assert payload["files_per_distinct_value_median"] == 4.0
        assert payload["records_per_distinct_value_median"] == 400.0
        assert len(payload["top_values"]) == 3

        report = client.get(f"/api/runs/{run_id}/report").json()
        assert report["overall_status"] == "needs_attention"
        partition = report["future_table_design"]["partition_spec"]
        assert partition["status"] == "consider"
        assert partition["recommendation"] == "days(event_timestamp)"
        assert "R000" in partition["evidence"]
        assert "R002" in partition["evidence"]
        assert "R003" in partition["evidence"]
        assert "R004" in partition["evidence"]
        assert len(models[0].requests) == 6


def _assert_prompt_carries_candidate_workflow(surface: dict[str, str]) -> None:
    """The delivered system prompt carries the partition-candidate evidence
    workflow: metadata first, targeted only when insufficient, current-spec
    comparison, and the supported/consider/insufficient_evidence reporting
    vocabulary."""
    system = surface["system"]
    assert "Partition-candidate evidence workflow" in system
    assert "get_column_metadata_metrics" in system
    assert "analyze_partition_candidate" in system
    assert "get_partition_layout" in system
    assert "get_partition_spec_usage" in system
    assert "supported" in system
    assert "consider" in system
    assert "insufficient_evidence" in system
    assert "never broad profiling" in system
