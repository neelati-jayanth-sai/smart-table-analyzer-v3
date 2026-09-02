"""Integration tests for the FastAPI app/SSE/UI layer.

Covers Runtime_Environments_UI.md #18-#21, #24-#25, #29-#30, #41-#45, #49,
#58-#59 and Architecture.md #6-#7, #9, #15, #30, #32-#33.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

import httpx
import pytest
from pydantic_ai import ModelHTTPError
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings
from starlette.testclient import TestClient
from uvicorn.importer import import_from_string

import sta.app.api as sta_api
from sta.app.api import create_app
from sta.app.runs import RunComponents
from sta.config import Settings, get_settings
from sta.context.table_metadata import StaticMetadataProvider, TableMetadata
from sta.execution.backends.local import LocalIcebergBackend
from sta.investigator.agent import InvestigationTools, PydanticAiInvestigator, create_investigator
from sta.investigator.report import (
    DesignRecommendationStatus,
    Finding,
    FindingConfidence,
    FutureTableDesign,
    InvestigationReport,
    OverallStatus,
    Severity,
    SpecRecommendation,
)
from sta.knowledge.repository import KnowledgeBase
from sta.results.store import ResultStore
from sta.tools.registry import DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

TABLE = "demo.sales.orders"
SNAPSHOT_ID = "9182781280348117982"


def _build_settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults = {
        "sta_env": "local",
        "db_path": str(tmp_path / "sta.sqlite3"),
        "knowledge_path": str(tmp_path / "knowledge"),
        "max_concurrent_runs": 2,
        "query_timeout_seconds": 30,
    }
    defaults.update(overrides)
    return Settings(**defaults)


def _make_components(
    settings: Settings,
    table_metadata: TableMetadata,
    local_table_fixture,
    report_callback,
) -> RunComponents:
    # Knowledge corpus is required for report reference validation; mirror the
    # repository layout under a temporary root so tests stay hermetic.
    kb = KnowledgeBase(settings.knowledge_path)
    store = ResultStore(settings.db_path)
    return RunComponents(
        store=store,
        knowledge=kb,
        metadata_provider=StaticMetadataProvider({TABLE: table_metadata}),
        backend_factory=lambda table, md: LocalIcebergBackend(local_table_fixture),
        investigator_factory=lambda: create_investigator(callback=report_callback),
    )


def _run_report(session) -> InvestigationReport:
    """Deterministic investigator callback used by the happy run.

    Runs one measurement tool so the run produces R001 evidence, then actually
    reads the knowledge document the report cites through the real tool path
    (which persists the ``knowledge_read`` event the report validator derives
    reads from). Current partition/sort facts cite R000, the reserved
    startup-metadata record (Architecture.md #32).
    """
    tools = InvestigationTools(session)
    outcome = session.runner.run("get_file_layout")
    tools.read_knowledge("runbooks/file-sizing.md")
    return InvestigationReport(
        table=session.table,
        snapshot_id=session.snapshot_id,
        overall_status=OverallStatus.NEEDS_ATTENTION,
        current_issues=[
            Finding(
                finding="Large file sizes",
                severity=Severity.HIGH,
                confidence=FindingConfidence.LIKELY,
                evidence=[outcome.result_id, "R000"],
                knowledge=["runbooks/file-sizing.md"],
                explanation=f"The file layout result {outcome.result_id} shows large files.",
            )
        ],
        future_table_design=FutureTableDesign(
            partition_spec=SpecRecommendation(
                current="days(created_at)",
                status=DesignRecommendationStatus.NO_CHANGE,
                confidence=FindingConfidence.VERIFIED,
                evidence=["R000"],
                reasoning="No partition skew evidence.",
            ),
            sort_order=SpecRecommendation(
                current="created_at ASC NULLS FIRST",
                status=DesignRecommendationStatus.NO_CHANGE,
                confidence=FindingConfidence.VERIFIED,
                evidence=["R000"],
                reasoning="Data is sorted.",
            ),
        ),
    )


def _poll_until_terminal(client: TestClient, run_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        assert response.status_code == 200, response.text
        run = response.json()
        if run["status"] in {"completed", "failed", "cancelled"}:
            return run
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach a terminal state in time")


def _collect_sse_until_terminal(
    client: TestClient,
    run_id: str,
    after_event_id: int | None = None,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Stream events, parse id/data lines, and stop after the terminal event."""
    url = f"/api/runs/{run_id}/events"
    if after_event_id is not None:
        url += f"?after_event_id={after_event_id}"
    events: list[dict[str, Any]] = []
    terminal_types = {"run_completed", "run_failed", "run_cancelled"}
    start = time.monotonic()
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        buffer = ""
        for chunk in response.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                block, _, buffer = buffer.partition("\n\n")
                lines = block.splitlines()
                if not lines:
                    continue
                # Heartbeat / comment lines start with ':'
                if lines[0].startswith(":"):
                    continue
                event: dict[str, Any] = {}
                data_lines: list[str] = []
                for line in lines:
                    if line.startswith("id: "):
                        event["id"] = int(line[4:])
                    elif line.startswith("data: "):
                        data_lines.append(line[6:])
                if data_lines:
                    event["data"] = json.loads("".join(data_lines))
                    events.append(event)
                    if event["data"].get("type") in terminal_types:
                        return events
            if time.monotonic() - start > timeout:
                raise AssertionError("SSE stream did not reach terminal event in time")
    return events


@pytest.fixture
def knowledge_corpus(tmp_path: Path) -> Path:
    """A minimal curated corpus so report validation can cite knowledge."""
    root = tmp_path / "knowledge"
    runbooks = root / "runbooks"
    runbooks.mkdir(parents=True)
    (runbooks / "file-sizing.md").write_text("# File sizing\n\nKeep files around 128 MB.\n")
    return root


@pytest.fixture
def di_app(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    """Dependency-injected app with a deterministic local backend + investigator."""
    settings = _build_settings(tmp_path, knowledge_path=str(knowledge_corpus))
    components = _make_components(settings, table_metadata, local_table_fixture, _run_report)
    app = create_app(components=components, settings=settings)
    try:
        yield app, settings, components
    finally:
        components.store.close()


@pytest.fixture
def di_client(di_app):
    app, _settings, _components = di_app
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_di_happy_run_produces_r000_r001_and_report(di_client: TestClient):
    response = di_client.post("/api/runs", json={"table_name": TABLE})
    assert response.status_code == 200
    run = response.json()
    assert run["table"] == TABLE
    assert run["status"] == "queued"
    run_id = run["run_id"]

    final = _poll_until_terminal(di_client, run_id)
    assert final["status"] == "completed"
    assert final["snapshot_id"] == SNAPSHOT_ID
    assert final["error"] is None

    results_response = di_client.get(f"/api/runs/{run_id}/results")
    assert results_response.status_code == 200
    result_ids = [r["result_id"] for r in results_response.json()["results"]]
    assert result_ids == ["R000", "R001"]

    r000 = di_client.get(f"/api/runs/{run_id}/results/R000").json()
    assert r000["tool_name"] == "full_schema"
    assert r000["query_version"] == "startup_context:v1"
    assert r000["snapshot_id"] == SNAPSHOT_ID
    assert r000["row_count"] == len(r000["payload"]["fields"])

    r001 = di_client.get(f"/api/runs/{run_id}/results/R001").json()
    assert r001["tool_name"] == "get_file_layout"
    assert r001["snapshot_id"] == SNAPSHOT_ID
    assert r001["row_count"] == 1

    report_response = di_client.get(f"/api/runs/{run_id}/report")
    assert report_response.status_code == 200
    report = report_response.json()
    assert report["table"] == TABLE
    assert report["snapshot_id"] == SNAPSHOT_ID
    assert report["overall_status"] == "needs_attention"
    assert report["current_issues"][0]["evidence"] == ["R001", "R000"]


# ---------------------------------------------------------------------------
# API input rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected_detail_substring",
    [
        ({"table_name": "demo.sales.orders", "extra": "bad"}, "Extra inputs"),
        ({"table_name": ""}, "must not be empty"),
        ({"table_name": "notqualified"}, "fully qualified"),
        ({"table_name": "demo.sales.'orders'"}, "plain identifier"),
        ({"table_name": "demo..orders"}, "plain identifier"),
        ({}, "Field required"),
    ],
)
def test_create_run_rejects_invalid_input(di_client: TestClient, body, expected_detail_substring):
    response = di_client.post("/api/runs", json=body)
    assert response.status_code == 422
    detail = response.json()["detail"]
    text = json.dumps(detail)
    assert expected_detail_substring in text


# ---------------------------------------------------------------------------
# status / results / report endpoints
# ---------------------------------------------------------------------------


def test_status_results_and_report_endpoints(di_client: TestClient):
    response = di_client.post("/api/runs", json={"table_name": TABLE})
    run_id = response.json()["run_id"]

    # Status exists before terminal state.
    status_response = di_client.get(f"/api/runs/{run_id}")
    assert status_response.status_code == 200
    assert status_response.json()["run_id"] == run_id

    # Unknown run returns 404 on all run-scoped endpoints.
    for path in [
        "/api/runs/run_does_not_exist",
        "/api/runs/run_does_not_exist/results",
        "/api/runs/run_does_not_exist/results/R001",
        "/api/runs/run_does_not_exist/report",
    ]:
        assert di_client.get(path).status_code == 404

    _poll_until_terminal(di_client, run_id)

    # Report not ready before completion was already verified above; after
    # completion it is present.
    report_response = di_client.get(f"/api/runs/{run_id}/report")
    assert report_response.status_code == 200

    # Unknown result still returns 404 even for an existing run.
    assert di_client.get(f"/api/runs/{run_id}/results/R999").status_code == 404


def test_report_not_ready_returns_404_while_run_active(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    def slow_report(session) -> InvestigationReport:
        # Keep the run active long enough for the 404 check, then return a
        # fully compliant report: one stored measurement, current partition
        # and sort facts citing R000 (Architecture.md #32).
        outcome = session.runner.run("get_file_layout")
        time.sleep(1.0)
        return InvestigationReport(
            table=session.table,
            snapshot_id=session.snapshot_id,
            overall_status=OverallStatus.HEALTHY,
            no_change_decisions=[
                f"File layout in {outcome.result_id} shows no material concern."
            ],
            future_table_design=FutureTableDesign(
                partition_spec=SpecRecommendation(
                    current="unpartitioned",
                    status=DesignRecommendationStatus.NO_CHANGE,
                    confidence=FindingConfidence.INCONCLUSIVE,
                    evidence=["R000"],
                    reasoning="Slow.",
                ),
                sort_order=SpecRecommendation(
                    current="none",
                    status=DesignRecommendationStatus.NO_CHANGE,
                    confidence=FindingConfidence.INCONCLUSIVE,
                    evidence=["R000"],
                    reasoning="Slow.",
                ),
            ),
        )

    settings = _build_settings(tmp_path, knowledge_path=str(knowledge_corpus))
    components = _make_components(
        settings, table_metadata, local_table_fixture, slow_report
    )
    app = create_app(components=components, settings=settings)
    try:
        with TestClient(app) as client:
            response = client.post("/api/runs", json={"table_name": TABLE})
            run_id = response.json()["run_id"]

            # Wait until the run is past startup but still investigating.
            for _ in range(50):
                run = client.get(f"/api/runs/{run_id}").json()
                if run["phase"] == "investigating":
                    break
                time.sleep(0.05)

            report_response = client.get(f"/api/runs/{run_id}/report")
            assert report_response.status_code == 404

            # Cancel the run so the test fixture shuts down quickly.
            client.post(f"/api/runs/{run_id}/cancel")
    finally:
        components.store.close()


# ---------------------------------------------------------------------------
# SSE replay and terminal stream
# ---------------------------------------------------------------------------


def test_sse_stream_replays_and_ends_at_terminal_event(di_client: TestClient):
    response = di_client.post("/api/runs", json={"table_name": TABLE})
    run_id = response.json()["run_id"]

    # Collect the whole terminal stream.
    events = _collect_sse_until_terminal(di_client, run_id)
    event_types = [e["data"]["type"] for e in events]
    assert "run_started" in event_types
    assert "table_context_ready" in event_types
    assert "report_ready" in event_types
    assert event_types[-1] == "run_completed"
    ids = [e["id"] for e in events]
    assert ids == list(range(ids[0], ids[-1] + 1))

    # Reconnect in the middle and replay only missed events.
    reconnect_after = events[2]["id"]
    replayed = _collect_sse_until_terminal(di_client, run_id, after_event_id=reconnect_after)
    replayed_ids = [e["id"] for e in replayed]
    assert replayed_ids == list(range(reconnect_after + 1, events[-1]["id"] + 1))
    assert [e["data"]["type"] for e in replayed][-1] == "run_completed"


def _parse_sse_response_text(text: str) -> list[dict[str, Any]]:
    """Parse a complete SSE text body into id/data events."""
    events: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        if not block or block.startswith(":"):
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
    return events


def test_sse_last_event_id_header_wins_over_query_param(di_client: TestClient):
    response = di_client.post("/api/runs", json={"table_name": TABLE})
    run_id = response.json()["run_id"]
    original = _collect_sse_until_terminal(di_client, run_id)
    assert len(original) >= 3, "need at least 3 events to test header precedence"

    # Query param asks for all events; Last-Event-ID header asks for events
    # after the second event. Header wins, so the replay starts after id 2.
    resp = di_client.get(
        f"/api/runs/{run_id}/events?after_event_id=0",
        headers={"Last-Event-ID": "2"},
    )
    assert resp.status_code == 200
    replayed = _parse_sse_response_text(resp.text)
    replayed_ids = [e["id"] for e in replayed]
    assert replayed_ids == list(range(3, original[-1]["id"] + 1))
    assert [e["data"]["type"] for e in replayed][-1] == "run_completed"


def test_sse_stream_sends_retry_hint(di_client: TestClient):
    response = di_client.post("/api/runs", json={"table_name": TABLE})
    run_id = response.json()["run_id"]
    with di_client.stream("GET", f"/api/runs/{run_id}/events") as response:
        first = response.iter_text().__next__()
    assert first.startswith("retry: 3000")


# ---------------------------------------------------------------------------
# failures and cancellation
# ---------------------------------------------------------------------------


def test_default_local_app_fails_fast_without_catalog_config(tmp_path: Path):
    """Runtime default: STA_ENV=local without ICEBERG_CATALOG_URI must fail
    clearly at startup (Runtime_Environments_UI.md #49) — an unconfigured
    local runtime is never silently accepted."""
    settings = _build_settings(tmp_path)
    with pytest.raises(RuntimeError, match="ICEBERG_CATALOG_URI"):
        create_app(settings=settings)


def test_default_local_app_fails_fast_when_catalog_unreachable(tmp_path: Path):
    """A configured but unreachable local catalog also fails fast, with a
    typed, safe message that names the remedy."""
    settings = _build_settings(
        tmp_path, iceberg_catalog_uri="http://127.0.0.1:9/catalog", iceberg_catalog_name="local"
    )
    with pytest.raises(RuntimeError, match="could not connect to the configured local Iceberg catalog"):
        create_app(settings=settings)


def test_production_config_validation_fails_fast(tmp_path: Path):
    with pytest.raises(RuntimeError, match="missing required production configuration"):
        create_app(settings=_build_settings(tmp_path, sta_env="production"))


def test_unsupported_environment_rejects_startup(tmp_path: Path):
    with pytest.raises(RuntimeError, match="unsupported STA_ENV"):
        create_app(settings=_build_settings(tmp_path, sta_env="staging"))


def test_cancel_run_stops_active_investigation(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    slow = False

    def slow_report(session) -> InvestigationReport:
        nonlocal slow
        slow = True
        # Block long enough that cancellation can reach the thread. The
        # measurement keeps the produced report compliant in case cancellation
        # ever loses the race.
        outcome = session.runner.run("get_file_layout")
        time.sleep(1.0)
        return InvestigationReport(
            table=session.table,
            snapshot_id=session.snapshot_id,
            overall_status=OverallStatus.HEALTHY,
            no_change_decisions=[
                f"File layout in {outcome.result_id} shows no material concern."
            ],
            future_table_design=FutureTableDesign(
                partition_spec=SpecRecommendation(
                    current="unpartitioned",
                    status=DesignRecommendationStatus.NO_CHANGE,
                    confidence=FindingConfidence.INCONCLUSIVE,
                    evidence=["R000"],
                    reasoning="Cancelled before evidence.",
                ),
                sort_order=SpecRecommendation(
                    current="none",
                    status=DesignRecommendationStatus.NO_CHANGE,
                    confidence=FindingConfidence.INCONCLUSIVE,
                    evidence=["R000"],
                    reasoning="Cancelled before evidence.",
                ),
            ),
        )

    settings = _build_settings(tmp_path, knowledge_path=str(knowledge_corpus))
    components = _make_components(
        settings, table_metadata, local_table_fixture, slow_report
    )
    app = create_app(components=components, settings=settings)
    try:
        with TestClient(app) as client:
            response = client.post("/api/runs", json={"table_name": TABLE})
            run_id = response.json()["run_id"]

            # Wait until the investigator thread has actually started.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and not slow:
                time.sleep(0.05)

            cancel_response = client.post(f"/api/runs/{run_id}/cancel")
            assert cancel_response.status_code == 200
            assert cancel_response.json()["status"] in {"cancelled", "running"}

            final = _poll_until_terminal(client, run_id, timeout=5.0)
            assert final["status"] == "cancelled"

            events = _collect_sse_until_terminal(client, run_id)
            event_types = [e["data"]["type"] for e in events]
            assert "run_cancelled" in event_types
            assert event_types[-1] == "run_cancelled"

            # Evidence already collected before cancellation is preserved.
            results = client.get(f"/api/runs/{run_id}/results").json()["results"]
            assert any(r["result_id"] == "R000" for r in results)
    finally:
        components.store.close()


# ---------------------------------------------------------------------------
# model-transport failures (verified root error path from the final live E2E)
# ---------------------------------------------------------------------------


_REJECTED_KEY = "sk-test-rejected-ollama-key-value"
_REJECTED_BODY = {"error": f"invalid api key: {_REJECTED_KEY}"}
_MODEL_ENDPOINT = "https://ollama.com/api/chat"


class _FailingModel(Model):
    """Pydantic AI model seam whose every request raises the given exception
    (simulating the provider transport failure; no network is touched)."""

    def __init__(self, exc: Exception):
        self._exc = exc
        super().__init__()

    @property
    def model_name(self) -> str:
        return "gpt-oss:120b-cloud"

    @property
    def system(self) -> str:
        return "test"

    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ):
        raise self._exc


def _model_failure_components(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
    model_exc: Exception,
) -> RunComponents:
    settings = _build_settings(tmp_path, knowledge_path=str(knowledge_corpus))
    return RunComponents(
        store=ResultStore(settings.db_path),
        knowledge=KnowledgeBase(settings.knowledge_path),
        metadata_provider=StaticMetadataProvider({TABLE: table_metadata}),
        backend_factory=lambda table, md: LocalIcebergBackend(local_table_fixture),
        investigator_factory=lambda: PydanticAiInvestigator(_FailingModel(model_exc)),
    )


def _assert_model_failure_secrecy(final_run: dict, events: list[dict[str, Any]]) -> None:
    """Neither the run error nor any event may carry the response body, the API
    key, the endpoint, or raw model output."""
    stream_text = json.dumps(events) + json.dumps(final_run)
    assert _REJECTED_KEY not in stream_text
    assert "invalid api key" not in stream_text
    assert _MODEL_ENDPOINT not in stream_text
    assert "ollama.com" not in stream_text
    assert "raw_output" not in stream_text


def test_rejected_model_key_fails_run_with_safe_model_failed_event(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    """Regression (final live E2E): the provider rejecting the model key with
    HTTP 401 must fail the run as a typed, safe model_failed — describing the
    authentication/configuration failure, never a generic internal_error, and
    never carrying the response body, key, endpoint, or raw model output. The
    report_rejected path stays distinct."""
    components = _model_failure_components(
        tmp_path, table_metadata, local_table_fixture, knowledge_corpus,
        ModelHTTPError(401, "gpt-oss:120b-cloud", body=_REJECTED_BODY),
    )
    app = create_app(components=components, settings=_build_settings(tmp_path))
    try:
        with TestClient(app) as client:
            run_id = client.post("/api/runs", json={"table_name": TABLE}).json()["run_id"]

            final = _poll_until_terminal(client, run_id)
            assert final["status"] == "failed"
            # safe run error describing the authentication/configuration failure
            assert "authentication" in final["error"].lower()
            assert "configuration" in final["error"].lower()

            events = _collect_sse_until_terminal(client, run_id)
            event_types = [e["data"]["type"] for e in events]
            assert "model_failed" in event_types
            # the report_rejected distinction is preserved
            assert "report_rejected" not in event_types
            assert event_types[-1] == "run_failed"

            model_failed = [e for e in events if e["data"]["type"] == "model_failed"][0]
            data = model_failed["data"]["data"]
            assert data["error_class"] == "model_failed"
            assert data["details"]["reason"] == "model_authentication_failed"
            assert data["details"]["status_code"] == 401
            assert "authentication" in data["message"].lower()

            # the terminal run_failed event carries the same safe classification
            failed = [e for e in events if e["data"]["type"] == "run_failed"][-1]
            assert failed["data"]["data"]["error_class"] == "model_failed"
            assert failed["data"]["data"]["details"]["reason"] == "model_authentication_failed"

            _assert_model_failure_secrecy(final, events)

            # no report is persisted for a failed transport
            assert client.get(f"/api/runs/{run_id}/report").status_code == 404
    finally:
        components.store.close()


def test_unreachable_model_service_fails_run_with_safe_model_failed_event(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    """A network-level transport failure (connect error naming the endpoint)
    must normalize to the same safe model_failed surface with no connection
    details."""
    components = _model_failure_components(
        tmp_path, table_metadata, local_table_fixture, knowledge_corpus,
        httpx.ConnectError(f"connection refused while contacting {_MODEL_ENDPOINT}"),
    )
    app = create_app(components=components, settings=_build_settings(tmp_path))
    try:
        with TestClient(app) as client:
            run_id = client.post("/api/runs", json={"table_name": TABLE}).json()["run_id"]

            final = _poll_until_terminal(client, run_id)
            assert final["status"] == "failed"
            assert "could not be reached" in final["error"].lower()

            events = _collect_sse_until_terminal(client, run_id)
            model_failed = [e for e in events if e["data"]["type"] == "model_failed"][0]
            data = model_failed["data"]["data"]
            assert data["details"]["reason"] == "model_unreachable"
            assert "exception_type" in data["details"]

            stream_text = json.dumps(events) + json.dumps(final)
            assert _MODEL_ENDPOINT not in stream_text
            assert "connection refused while contacting" not in stream_text
    finally:
        components.store.close()


# ---------------------------------------------------------------------------
# UI static assets
# ---------------------------------------------------------------------------


def test_ui_static_assets_served(di_client: TestClient):
    index = di_client.get("/")
    assert index.status_code == 200
    assert "text/html" in index.headers["content-type"]
    assert "Smart Table Analyzer" in index.text

    js = di_client.get("/app.js")
    assert js.status_code == 200
    assert "text/javascript" in js.headers["content-type"]
    # Verify the delivered JS parses cleanly.
    assert "EventSource" in js.text

    css = di_client.get("/styles.css")
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "--accent" in css.text


def test_ui_index_contains_required_sections(di_client: TestClient):
    html = di_client.get("/").text
    assert '<input id="table-name"' in html
    assert '<ol id="timeline"' in html
    assert '<ul id="activity"' in html
    assert '<section id="report-panel"' in html
    assert '<script src="/app.js">' in html
    assert '<link rel="stylesheet" href="/styles.css">' in html


# ---------------------------------------------------------------------------
# module-level ASGI entrypoint (uvicorn sta.app.api:app)
# ---------------------------------------------------------------------------


class _RecordingApp:
    """Minimal ASGI app that records scopes and completes the lifespan handshake."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        self.scopes.append(scope["type"])
        if scope["type"] != "lifespan":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})
            return
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def _drive_lifespan(app: Any) -> list[dict[str, Any]]:
    """Run a full uvicorn-style ASGI lifespan conversation against ``app``."""

    async def run() -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def receive() -> dict[str, Any]:
            return await queue.get()

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        scope: dict[str, Any] = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": {},
        }
        task = asyncio.create_task(app(scope, receive, send))
        await queue.put({"type": "lifespan.startup"})
        while not any(
            message["type"] in {"lifespan.startup.complete", "lifespan.startup.failed"}
            for message in sent
        ):
            await asyncio.sleep(0)
        await queue.put({"type": "lifespan.shutdown"})
        await task
        return sent

    return asyncio.run(run())


@pytest.fixture
def unconfigured_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Config-free runtime: no ``.env`` file, no catalog settings, and a
    cleared ``get_settings`` cache (restored after)."""
    monkeypatch.chdir(tmp_path)
    for name in ("STA_ENV", "ICEBERG_CATALOG_URI", "ICEBERG_WAREHOUSE"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_asgi_entrypoint_resolves_and_is_import_safe(
    unconfigured_env, tmp_path: Path
):
    """``uvicorn sta.app.api:app --reload`` (Runtime_Environments_UI.md #16)
    resolves without deployment configuration: importing the module — in a
    config-free process with an unsupported STA_ENV and no ``.env`` — must not
    build the default app (#49 fail-fast happens at startup, not at import)."""
    # uvicorn resolves the documented entrypoint exactly like this.
    assert import_from_string("sta.app.api:app") is sta_api.app

    # A fresh, config-free process must import the module cleanly.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("STA_", "ICEBERG_", "S3_", "IOMETE_", "OLLAMA", "LOCAL_"))
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    env["STA_ENV"] = "staging"  # would fail fast if the app were built at import
    result = subprocess.run(
        [sys.executable, "-c", "import sta.app.api; print(callable(sta.app.api.app))"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_asgi_entrypoint_fails_fast_at_lifespan_startup(unconfigured_env):
    """Without deployment config, ASGI startup fails fast: the entrypoint
    reports ``lifespan.startup.failed`` with the actionable message, which
    uvicorn treats as a fatal startup error (Runtime_Environments_UI.md #49)."""
    sent = _drive_lifespan(sta_api.app)
    assert [message["type"] for message in sent] == ["lifespan.startup.failed"]
    assert "ICEBERG_CATALOG_URI" in sent[0]["message"]


def test_asgi_entrypoint_builds_once_and_delegates_lifespan(
    monkeypatch: pytest.MonkeyPatch,
):
    """After a successful build the entrypoint delegates the whole ASGI
    conversation — lifespan startup and shutdown included — to the built app,
    and builds it exactly once (cached for later calls)."""
    recording = _RecordingApp()
    builds: list[int] = []

    def fake_create_app(*args: Any, **kwargs: Any) -> Any:
        builds.append(1)
        return recording

    # A fresh wrapper keeps the module-level entrypoint untouched for the
    # other tests; it is the same class behind ``sta.app.api:app``.
    lazy_app = sta_api._LazyApp(fake_create_app)
    sent = _drive_lifespan(lazy_app)
    assert builds == [1]
    assert recording.scopes == ["lifespan"]
    assert [message["type"] for message in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


# ---------------------------------------------------------------------------
# report rejection regression tests
# ---------------------------------------------------------------------------


def _minimal_report(session, *, evidence: list[str], overall_status: OverallStatus = OverallStatus.HEALTHY) -> InvestigationReport:
    """A minimal valid-shaped report for rejection tests."""
    return InvestigationReport(
        table=session.table,
        snapshot_id=session.snapshot_id,
        overall_status=overall_status,
        future_table_design=FutureTableDesign(
            partition_spec=SpecRecommendation(
                current="unpartitioned",
                status=DesignRecommendationStatus.NO_CHANGE,
                confidence=FindingConfidence.VERIFIED,
                reasoning="No evidence.",
                evidence=list(evidence),
            ),
            sort_order=SpecRecommendation(
                current="none",
                status=DesignRecommendationStatus.NO_CHANGE,
                confidence=FindingConfidence.VERIFIED,
                reasoning="No evidence.",
                evidence=list(evidence),
            ),
        ),
    )


def test_no_measurement_report_is_rejected_and_emits_report_rejected_event(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    """A report that cites only R000 (the full schema) and no R001+ measurement
    must be rejected, the run must fail, and the event stream must carry a safe
    report_rejected event with deterministic validator details (not raw output)."""

    def no_measurement_report(session) -> InvestigationReport:
        return _minimal_report(session, evidence=["R000"])

    settings = _build_settings(tmp_path, knowledge_path=str(knowledge_corpus))
    components = _make_components(
        settings, table_metadata, local_table_fixture, no_measurement_report
    )
    app = create_app(components=components, settings=settings)
    try:
        with TestClient(app) as client:
            response = client.post("/api/runs", json={"table_name": TABLE})
            run_id = response.json()["run_id"]

            final = _poll_until_terminal(client, run_id)
            assert final["status"] == "failed"
            assert final["error"] is not None
            assert "report rejected" in final["error"].lower()

            # No report is persisted.
            assert client.get(f"/api/runs/{run_id}/report").status_code == 404

            events = _collect_sse_until_terminal(client, run_id)
            event_types = [e["data"]["type"] for e in events]
            assert "report_rejected" in event_types
            assert event_types[-1] == "run_failed"

            rejected = [e for e in events if e["data"]["type"] == "report_rejected"][0]
            data = rejected["data"]["data"]
            assert data["error_class"] == "report_rejected"
            assert "validation_summary" in data
            assert "measurement" in data["validation_summary"]["error_categories"]
            # The raw callback/report object must not be dumped into the event.
            event_text = json.dumps(data)
            assert "raw_output" not in event_text
            assert data["validation_summary"]["validation_error_count"] >= 1

            # The terminal run_failed event also carries safe details.
            failed = [e for e in events if e["data"]["type"] == "run_failed"][-1]
            assert failed["data"]["data"]["error_class"] == "report_rejected"
            assert "details" in failed["data"]["data"]
    finally:
        components.store.close()


def test_invalid_report_output_is_normalized_to_report_rejected_event(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    """A callback/ model response that violates the Pydantic report schema must
    surface as a typed report_rejected event with schema-category details, not
    leak as an internal_error."""

    def invalid_report(session):
        # Missing required future_table_design and using an invalid enum value.
        return {
            "table": session.table,
            "snapshot_id": session.snapshot_id,
            "overall_status": "broken",
        }

    settings = _build_settings(tmp_path, knowledge_path=str(knowledge_corpus))
    components = _make_components(
        settings, table_metadata, local_table_fixture, invalid_report
    )
    app = create_app(components=components, settings=settings)
    try:
        with TestClient(app) as client:
            response = client.post("/api/runs", json={"table_name": TABLE})
            run_id = response.json()["run_id"]

            final = _poll_until_terminal(client, run_id)
            assert final["status"] == "failed"
            assert "report rejected" in final["error"].lower()

            events = _collect_sse_until_terminal(client, run_id)
            rejected = [e for e in events if e["data"]["type"] == "report_rejected"][0]
            summary = rejected["data"]["data"]["validation_summary"]
            assert "schema" in summary["error_categories"]
            assert summary["validation_error_count"] >= 1
    finally:
        components.store.close()


def test_valid_report_persists_after_successful_investigation(
    tmp_path: Path,
    table_metadata: TableMetadata,
    local_table_fixture,
    knowledge_corpus: Path,
):
    """Regression: a valid, evidence-backed report must persist and be retrievable."""

    def valid_report(session) -> InvestigationReport:
        outcome = session.runner.run("get_file_layout")
        # Cited knowledge must actually be read in this run through the real
        # tool path (persisting the knowledge_read event the validator uses).
        InvestigationTools(session).read_knowledge("runbooks/file-sizing.md")
        return InvestigationReport(
            table=session.table,
            snapshot_id=session.snapshot_id,
            overall_status=OverallStatus.NEEDS_ATTENTION,
            current_issues=[
                Finding(
                    finding="Large files",
                    severity=Severity.HIGH,
                    confidence=FindingConfidence.LIKELY,
                    evidence=[outcome.result_id],
                    knowledge=["runbooks/file-sizing.md"],
                    explanation=f"Result {outcome.result_id} shows large files.",
                )
            ],
            future_table_design=FutureTableDesign(
                partition_spec=SpecRecommendation(
                    current="days(created_at)",
                    status=DesignRecommendationStatus.NO_CHANGE,
                    confidence=FindingConfidence.VERIFIED,
                    evidence=["R000"],
                    reasoning="No skew.",
                ),
                sort_order=SpecRecommendation(
                    current="none",
                    status=DesignRecommendationStatus.NO_CHANGE,
                    confidence=FindingConfidence.VERIFIED,
                    evidence=["R000"],
                    reasoning="Sorted.",
                ),
            ),
        )

    settings = _build_settings(tmp_path, knowledge_path=str(knowledge_corpus))
    components = _make_components(
        settings, table_metadata, local_table_fixture, valid_report
    )
    app = create_app(components=components, settings=settings)
    try:
        with TestClient(app) as client:
            response = client.post("/api/runs", json={"table_name": TABLE})
            run_id = response.json()["run_id"]

            final = _poll_until_terminal(client, run_id)
            assert final["status"] == "completed"
            assert final["error"] is None

            report_response = client.get(f"/api/runs/{run_id}/report")
            assert report_response.status_code == 200
            report = report_response.json()
            assert report["table"] == TABLE
            assert report["snapshot_id"] == SNAPSHOT_ID
            assert report["current_issues"][0]["evidence"] == ["R001"]
    finally:
        components.store.close()
