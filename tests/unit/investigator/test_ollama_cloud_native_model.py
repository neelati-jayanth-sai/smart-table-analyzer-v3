"""Unit tests for the Ollama Cloud native ``/api/chat`` Pydantic AI adapter.

All HTTP traffic is mocked with ``respx``/``httpx`` transport patches; no real
Ollama Cloud calls happen here. Tests are plain sync pytest functions that
use ``asyncio.run`` for the async adapter surface, avoiding an async pytest
plugin dependency.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
import pytest
from pydantic_ai import ModelHTTPError, UserError
from pydantic_ai.messages import (
    InstructionPart,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.tools import ToolDefinition

from sta.investigator.ollama_cloud_native_model import (
    OLLAMA_CLOUD_NATIVE_URL,
    OllamaCloudNativeModel,
    SUPPORTED_MODEL_NAME,
)

SECRET = "sk-test-ollama-cloud-native"


@pytest.fixture
def model():
    return OllamaCloudNativeModel(api_key=SECRET)


def _run(coro):
    return asyncio.run(coro)


def _fake_ollama_response(
    content: str | None = None,
    tool_calls: list[dict] | None = None,
    done_reason: str = "stop",
    prompt_eval_count: int = 10,
    eval_count: int = 5,
) -> dict:
    message: dict = {"role": "assistant"}
    if content:
        message["content"] = content
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "model": SUPPORTED_MODEL_NAME,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "done": True,
        "done_reason": done_reason,
        "prompt_eval_count": prompt_eval_count,
        "eval_count": eval_count,
    }


def _intercept_post(response_json: dict, status_code: int = 200):
    """Return a context manager that patches ``httpx.AsyncClient.post``."""

    class _FakeResponse:
        def __init__(self, json_data, status):
            self._json = json_data
            self.status_code = status
            self.headers = {}
            self.text = json.dumps(json_data)

        def json(self):
            return self._json

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError(
                    "error",
                    request=httpx.Request("POST", OLLAMA_CLOUD_NATIVE_URL),
                    response=self,
                )

    return patch.object(
        httpx.AsyncClient,
        "post",
        return_value=_FakeResponse(response_json, status_code),
    )


def test_simple_text_request(model: OllamaCloudNativeModel):
    response_json = _fake_ollama_response(content="Hello")
    with _intercept_post(response_json):
        result = _run(
            model.request(
                messages=[ModelRequest(parts=[UserPromptPart(content="Hi")])],
                model_settings=None,
                model_request_parameters=ModelRequestParameters(),
            )
        )

    assert len(result.parts) == 1
    assert isinstance(result.parts[0], TextPart)
    assert result.parts[0].content == "Hello"
    assert result.model_name == SUPPORTED_MODEL_NAME
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 5


def test_system_and_user_messages_are_sent(model: OllamaCloudNativeModel):
    captured: dict | None = None
    captured_headers: dict | None = None

    class _FakeResponse:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return _fake_ollama_response(content="ok")

        def raise_for_status(self):
            pass

    async def capture_post(_self, _url, *, json=None, headers=None, **kwargs):
        nonlocal captured, captured_headers
        captured = json
        captured_headers = headers
        return _FakeResponse()

    with patch.object(httpx.AsyncClient, "post", capture_post):
        _run(
            model.request(
                messages=[
                    ModelRequest(
                        parts=[
                            SystemPromptPart(content="You are STA."),
                            UserPromptPart(content="Investigate."),
                        ]
                    )
                ],
                model_settings=None,
                model_request_parameters=ModelRequestParameters(),
            )
        )

    assert captured is not None
    assert captured["model"] == SUPPORTED_MODEL_NAME
    assert captured["stream"] is False
    messages = captured["messages"]
    assert messages[0] == {"role": "system", "content": "You are STA."}
    assert messages[1] == {"role": "user", "content": "Investigate."}
    assert captured_headers is not None
    assert captured_headers.get("Authorization") == f"Bearer {SECRET}"


def test_tool_call_response_is_parsed(model: OllamaCloudNativeModel):
    response_json = _fake_ollama_response(
        tool_calls=[
            {
                "id": "call_abc",
                "function": {
                    "name": "run_query",
                    "arguments": {"tool_name": "get_file_layout"},
                },
            }
        ]
    )
    with _intercept_post(response_json):
        result = _run(
            model.request(
                messages=[ModelRequest(parts=[UserPromptPart(content="Run a query")])],
                model_settings=None,
                model_request_parameters=ModelRequestParameters(),
            )
        )

    assert len(result.parts) == 1
    part = result.parts[0]
    assert isinstance(part, ToolCallPart)
    assert part.tool_name == "run_query"
    assert part.args == {"tool_name": "get_file_layout"}
    assert part.tool_call_id == "call_abc"


def test_tool_return_is_mapped_to_native_tool_message(model: OllamaCloudNativeModel):
    captured: dict | None = None

    class _FakeResponse:
        status_code = 200
        headers = {}

        def json(self):
            return _fake_ollama_response(content="done")

        def raise_for_status(self):
            pass

    async def capture_post(_self, _url, *, json=None, **kwargs):
        nonlocal captured
        captured = json
        return _FakeResponse()

    with patch.object(httpx.AsyncClient, "post", capture_post):
        _run(
            model.request(
                messages=[
                    ModelRequest(
                        parts=[
                            ToolReturnPart(
                                tool_name="run_query",
                                content={"result_id": "R001"},
                                tool_call_id="call_abc",
                            )
                        ]
                    )
                ],
                model_settings=None,
                model_request_parameters=ModelRequestParameters(),
            )
        )

    assert captured is not None
    tool_msg = captured["messages"][0]
    assert tool_msg["role"] == "tool"
    assert tool_msg["tool_name"] == "run_query"
    assert json.loads(tool_msg["content"]) == {"result_id": "R001"}


def test_tool_definitions_are_forwarded(model: OllamaCloudNativeModel):
    captured: dict | None = None

    class _FakeResponse:
        status_code = 200
        headers = {}

        def json(self):
            return _fake_ollama_response(content="ok")

        def raise_for_status(self):
            pass

    async def capture_post(_self, _url, *, json=None, **kwargs):
        nonlocal captured
        captured = json
        return _FakeResponse()

    tool = ToolDefinition(
        name="run_query",
        description="Run a predefined query",
        parameters_json_schema={
            "type": "object",
            "properties": {"tool_name": {"type": "string"}},
            "required": ["tool_name"],
        },
    )
    params = ModelRequestParameters(function_tools=[tool])

    with patch.object(httpx.AsyncClient, "post", capture_post):
        _run(
            model.request(
                messages=[ModelRequest(parts=[UserPromptPart(content="Hi")])],
                model_settings=None,
                model_request_parameters=params,
            )
        )

    assert captured is not None
    assert len(captured["tools"]) == 1
    assert captured["tools"][0]["function"]["name"] == "run_query"


def test_http_error_is_wrapped_as_model_http_error(model: OllamaCloudNativeModel):
    response_json = {"error": "Unauthorized"}
    with _intercept_post(response_json, status_code=401):
        with pytest.raises(ModelHTTPError) as exc_info:
            _run(
                model.request(
                    messages=[ModelRequest(parts=[UserPromptPart(content="Hi")])],
                    model_settings=None,
                    model_request_parameters=ModelRequestParameters(),
                )
            )
    assert exc_info.value.status_code == 401


def test_secret_key_is_not_in_repr(model: OllamaCloudNativeModel):
    text = repr(model) + str(model)
    assert SECRET not in text
    assert model.model_name == SUPPORTED_MODEL_NAME


def test_empty_api_key_raises_user_error():
    with pytest.raises(UserError):
        OllamaCloudNativeModel(api_key="")


def test_streaming_request_is_not_supported(model: OllamaCloudNativeModel):
    async def _stream():
        await model.request_stream(
            messages=[],
            model_settings=None,
            model_request_parameters=ModelRequestParameters(),
        )

    with pytest.raises(NotImplementedError):
        _run(_stream())
