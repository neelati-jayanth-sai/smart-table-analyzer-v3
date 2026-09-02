"""Unit tests for typed, safe model-transport failure normalization.

Verified from the final live E2E: the configured Ollama Cloud key reached the
native ``https://ollama.com/api/chat`` endpoint and was rejected with HTTP 401;
the raw Pydantic AI ``ModelHTTPError`` (which carries the provider response
body and headers) escaped :class:`PydanticAiInvestigator` and RunService could
only record a generic ``internal_error`` together with the response body.

Contract under test here:

- ``PydanticAiInvestigator`` maps ``ModelHTTPError`` and network-level
  ``httpx`` errors to the typed, safe :class:`ModelTransportError`
  (the original exception stays on ``__cause__`` for in-process diagnosis),
- 401/403 normalize to an authentication/configuration failure,
- the safe message/details never contain the response body, the API key, the
  endpoint, or raw model output.

No network calls happen in these tests: the model seam raises directly.
"""

import json

import httpx
import pytest
from pydantic_ai import ModelHTTPError
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings

from sta.context.table_context import TableContext
from sta.investigator.agent import (
    InvestigationSession,
    ModelTransportError,
    PydanticAiInvestigator,
    _model_transport_error_from_http_error,
    _model_transport_error_from_transport_error,
)
from sta.knowledge.repository import KnowledgeBase
from sta.results.models import RunRecord
from sta.results.store import ResultStore

TABLE = "demo.sales.orders"
SNAPSHOT = "9182781280348117982"
RUN_ID = "run_transport_test"

# Secret-shaped material that must never surface in the safe error/event path.
SECRET_KEY = "sk-live-rejected-ollama-key-value"
RESPONSE_BODY = '{"error": "invalid api key: ' + SECRET_KEY + '"}'
ENDPOINT = "https://ollama.com/api/chat"


class RaisingModel(Model):
    """Pydantic AI model seam whose every request raises the given exception."""

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


@pytest.fixture
def session(tmp_path) -> InvestigationSession:
    store = ResultStore(tmp_path / "results.sqlite3")
    store.create_run(RunRecord(run_id=RUN_ID, table=TABLE, snapshot_id=SNAPSHOT, status="running"))
    knowledge_root = tmp_path / "knowledge"
    knowledge_root.mkdir(parents=True)
    knowledge = KnowledgeBase(knowledge_root)
    try:
        yield InvestigationSession(
            run_id=RUN_ID,
            table=TABLE,
            snapshot_id=SNAPSHOT,
            table_context=TableContext(table=TABLE, schema_summary=[], column_groups={}),
            runner=None,
            store=store,
            knowledge=knowledge,
        )
    finally:
        store.close()


def assert_secret_free(text: str) -> None:
    """The sanitized surface must not carry body, key, endpoint, or raw output."""
    assert SECRET_KEY not in text
    assert "invalid api key" not in text
    assert ENDPOINT not in text
    assert "ollama.com" not in text


# ---------------------------------------------------------------------------
# direct normalization of the Pydantic AI exceptions
# ---------------------------------------------------------------------------


def test_401_normalizes_to_safe_authentication_failure():
    exc = ModelHTTPError(401, "gpt-oss:120b-cloud", body=RESPONSE_BODY)

    error = _model_transport_error_from_http_error(exc)

    assert isinstance(error, ModelTransportError)
    assert "authentication" in str(error)
    assert "configuration" in str(error)
    assert error.safe_details["reason"] == "model_authentication_failed"
    assert error.safe_details["status_code"] == 401
    assert_secret_free(str(error))
    assert_secret_free(json.dumps(error.safe_details))


def test_403_normalizes_to_safe_authentication_failure():
    error = _model_transport_error_from_http_error(
        ModelHTTPError(403, "gpt-oss:120b-cloud", body=RESPONSE_BODY)
    )

    assert error.safe_details["reason"] == "model_authentication_failed"
    assert "authentication" in str(error)
    assert_secret_free(str(error))


def test_other_http_status_normalizes_to_safe_http_failure():
    error = _model_transport_error_from_http_error(
        ModelHTTPError(503, "gpt-oss:120b-cloud", body=RESPONSE_BODY)
    )

    assert error.safe_details["reason"] == "model_http_error"
    assert error.safe_details["status_code"] == 503
    assert "503" in str(error)
    assert_secret_free(str(error))


def test_zero_status_normalizes_to_safe_unreachable_failure():
    error = _model_transport_error_from_http_error(
        ModelHTTPError(0, "gpt-oss:120b-cloud", body=RESPONSE_BODY)
    )

    assert error.safe_details["reason"] == "model_unreachable"
    assert "status_code" not in error.safe_details
    assert_secret_free(str(error))


def test_httpx_transport_error_normalizes_to_safe_unreachable_failure():
    error = _model_transport_error_from_transport_error(
        httpx.ConnectError(f"connection refused while contacting {ENDPOINT}")
    )

    assert isinstance(error, ModelTransportError)
    assert error.safe_details["reason"] == "model_unreachable"
    assert error.safe_details["exception_type"] == "ConnectError"
    assert_secret_free(str(error))


# ---------------------------------------------------------------------------
# mapping through the real PydanticAiInvestigator seam
# ---------------------------------------------------------------------------


def test_investigator_maps_model_http_error_to_safe_transport_error(session):
    investigator = PydanticAiInvestigator(
        RaisingModel(ModelHTTPError(401, "gpt-oss:120b-cloud", body=RESPONSE_BODY))
    )

    with pytest.raises(ModelTransportError) as exc_info:
        investigator.investigate(session)

    error = exc_info.value
    assert error.safe_details["reason"] == "model_authentication_failed"
    assert error.safe_details["status_code"] == 401
    # deterministic, secret-free surface
    assert_secret_free(str(error))
    assert_secret_free(json.dumps(error.safe_details))
    # the raw exception stays chained for in-process diagnosis only
    assert isinstance(error.__cause__, ModelHTTPError)
    assert SECRET_KEY in str(error.__cause__.body)


def test_investigator_maps_transport_error_to_safe_transport_error(session):
    investigator = PydanticAiInvestigator(
        RaisingModel(httpx.ConnectTimeout(f"timed out contacting {ENDPOINT}"))
    )

    with pytest.raises(ModelTransportError) as exc_info:
        investigator.investigate(session)

    error = exc_info.value
    assert error.safe_details["reason"] == "model_unreachable"
    assert_secret_free(str(error))
    assert isinstance(error.__cause__, httpx.HTTPError)


def test_investigator_mapping_is_distinct_from_report_rejection(session):
    """A transport failure must surface as ModelTransportError, never as the
    report_rejected path (and vice versa is covered by the loop tests)."""
    investigator = PydanticAiInvestigator(
        RaisingModel(ModelHTTPError(401, "gpt-oss:120b-cloud", body=RESPONSE_BODY))
    )

    with pytest.raises(ModelTransportError) as exc_info:
        investigator.investigate(session)

    from sta.investigator.agent import ReportRejectedError

    assert not isinstance(exc_info.value, ReportRejectedError)