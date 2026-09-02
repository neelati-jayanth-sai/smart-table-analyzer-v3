"""Custom Pydantic AI 2.37 Model adapter for Ollama Cloud's native ``/api/chat`` API.

Pydantic AI's bundled ``OllamaModel`` routes through the OpenAI-compatible
``/v1/chat/completions`` endpoint. The supplied Ollama Cloud key works only at
the native ``https://ollama.com/api/chat`` endpoint, so this adapter implements
the public Pydantic AI :class:`~pydantic_ai.models.Model` interface against that
native API.

Hard constraints:

- Exactly one model name is supported: ``gpt-oss:120b-cloud``.
- The API key is a secret: it travels only in the ``Authorization: Bearer``
  header and is excluded from ``repr``/``str``.
- Cloud native structured output does not enforce JSON Schema, so the adapter
  profile disables ``supports_json_schema_output`` and lets Pydantic AI fall
  back to ``ToolOutput`` structured output (the ``final_result`` tool), which is
  verified to work.
- Streaming, native tools, multimodal input, image/audio output, and token
  counting ahead of the request are explicitly not supported by this focused
  adapter; calling those paths raises clear ``UserError`` /
  ``NotImplementedError`` rather than pretending success.

The adapter supports sync agent runs via async requests, system/user messages,
tool definitions/tool calls/tool returns, and structured report output through
Pydantic AI's tool-output mechanism.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic_ai import ModelHTTPError, UnexpectedModelBehavior, UserError
from pydantic_ai.messages import (
    FinishReason,
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.profiles import DEFAULT_PROFILE, ModelProfile, merge_profile
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.usage import RequestUsage

OLLAMA_CLOUD_NATIVE_URL = "https://ollama.com/api/chat"
SUPPORTED_MODEL_NAME = "gpt-oss:120b-cloud"


def _json_serialize(value: Any) -> str:
    """Serialize a tool return payload to a JSON string suitable for Ollama's
    native ``role: tool`` ``content`` field."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _map_tool_definition(tool_def: ToolDefinition) -> dict[str, Any]:
    """Translate a Pydantic AI tool definition into Ollama native tool format."""
    return {
        "type": "function",
        "function": {
            "name": tool_def.name,
            "description": tool_def.description or "",
            "parameters": tool_def.parameters_json_schema,
        },
    }


def _map_messages(
    messages: Sequence[ModelMessage],
    params: ModelRequestParameters,
) -> list[dict[str, Any]]:
    """Translate Pydantic AI message history into Ollama native ``messages``."""
    ollama_messages: list[dict[str, Any]] = []

    for message in messages:
        if isinstance(message, ModelRequest):
            ollama_messages.extend(_map_request_message(message))
        elif isinstance(message, ModelResponse):
            mapped = _map_response_message(message)
            if mapped is not None:
                ollama_messages.append(mapped)
        else:  # pragma: no cover
            raise UnexpectedModelBehavior(f"Unsupported message type: {type(message).__name__}")

    # System instructions are supplied by Pydantic AI as instruction_parts on the
    # request parameters. Insert them as leading system messages unless the
    # history already carries an explicit system message at the front.
    instruction_parts = _get_instruction_parts(messages, params)
    if instruction_parts:
        if not ollama_messages or ollama_messages[0].get("role") != "system":
            for part in instruction_parts:
                ollama_messages.insert(0, {"role": "system", "content": part.content})

    return ollama_messages


def _get_instruction_parts(
    messages: Sequence[ModelMessage],
    params: ModelRequestParameters,
) -> list[SystemPromptPart] | None:
    """Return the structured instruction parts for this request.

    Pydantic AI 2.37 passes system/instructions as ``InstructionPart``
    instances in ``ModelRequestParameters.instruction_parts``. Convert any
    instruction part into a system message so Ollama's native ``/api/chat``
    receives the system prompt. If no instruction parts are supplied, fall
    back to the ``instructions`` string on the message history, mirroring the
    behavior of Pydantic AI's base ``Model``.
    """
    if params.instruction_parts is not None:
        parts: list[SystemPromptPart] = []
        for part in params.instruction_parts:
            if isinstance(part, SystemPromptPart):
                parts.append(part)
            elif isinstance(part, InstructionPart):
                parts.append(SystemPromptPart(content=part.content))
        return parts or None

    # Fallback: synthesize from message history.
    last_two_requests: list[ModelRequest] = []
    for msg in reversed(messages):
        if isinstance(msg, ModelRequest):
            last_two_requests.append(msg)
            if len(last_two_requests) == 2:
                break
            if msg.instructions is not None:
                return [SystemPromptPart(content=msg.instructions)]

    if len(last_two_requests) == 2:
        most_recent = last_two_requests[0]
        second = last_two_requests[1]
        if (
            all(p.part_kind in {"tool-return", "retry-prompt"} for p in most_recent.parts)
            and second.instructions is not None
        ):
            return [SystemPromptPart(content=second.instructions)]

    return None


def _map_request_message(message: ModelRequest) -> list[dict[str, Any]]:
    """Translate one ``ModelRequest`` into one or more Ollama native messages."""
    out: list[dict[str, Any]] = []

    for part in message.parts:
        if isinstance(part, SystemPromptPart):
            out.append({"role": "system", "content": part.content})
        elif isinstance(part, UserPromptPart):
            out.append(_map_user_prompt(part))
        elif isinstance(part, ToolReturnPart):
            content_str, _ = part.model_response_str_and_user_content()
            out.append(
                {
                    "role": "tool",
                    "tool_name": part.tool_name,
                    "content": content_str,
                }
            )
        elif isinstance(part, RetryPromptPart):
            out.append({"role": "user", "content": _retry_prompt_content(part)})
        else:  # pragma: no cover
            raise UnexpectedModelBehavior(f"Unsupported request part: {part.part_kind}")

    return out


def _map_user_prompt(part: UserPromptPart) -> dict[str, Any]:
    """Translate a user prompt part to an Ollama user message."""
    if isinstance(part.content, str):
        return {"role": "user", "content": part.content}

    # Multimodal input is not supported by this focused cloud adapter.
    raise UserError(
        f"{OllamaCloudNativeModel.__name__} only supports text user prompts, "
        f"got {type(part.content).__name__}"
    )


def _retry_prompt_content(part: RetryPromptPart) -> str:
    """Serialize a retry prompt to text."""
    content = part.content
    if isinstance(content, str):
        return content
    # Pydantic Core ErrorDetails list.
    return json.dumps(
        [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in content],
        ensure_ascii=False,
        default=str,
    )


def _map_response_message(message: ModelResponse) -> dict[str, Any] | None:
    """Translate a ``ModelResponse`` into an Ollama assistant message."""
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for part in message.parts:
        if isinstance(part, TextPart):
            content_parts.append(part.content)
        elif isinstance(part, ToolCallPart):
            tool_calls.append(_map_tool_call(part))
        elif isinstance(part, SystemPromptPart):  # pragma: no cover
            # Responses should not carry system parts, but handle safely.
            content_parts.append(part.content)
        else:  # pragma: no cover
            raise UnexpectedModelBehavior(f"Unsupported response part: {part.part_kind}")

    if not content_parts and not tool_calls:
        return None

    msg: dict[str, Any] = {"role": "assistant"}
    if content_parts:
        msg["content"] = "\n\n".join(content_parts)
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def _map_tool_call(part: ToolCallPart) -> dict[str, Any]:
    """Translate a Pydantic AI tool call into Ollama native tool-call format."""
    args = part.args
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError as exc:
            raise UnexpectedModelBehavior(
                f"Tool call {part.tool_name!r} contains invalid JSON arguments: {exc}"
            ) from exc
    elif not isinstance(args, dict):
        args = {"args": args}

    return {
        "id": part.tool_call_id,
        "function": {"name": part.tool_name, "arguments": args},
    }


def _parse_response(response_json: dict[str, Any]) -> ModelResponse:
    """Build a Pydantic AI ``ModelResponse`` from an Ollama native chat response."""
    message = response_json.get("message") or {}
    parts: list[ModelResponsePart] = []

    content = message.get("content")
    if content:
        parts.append(TextPart(content=content))

    for tc in message.get("tool_calls") or []:
        function = tc.get("function") or {}
        args = function.get("arguments")
        # Ollama native returns arguments as a JSON object, but be defensive.
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"raw": args}
        tool_call_id = tc.get("id") or tc.get("function", {}).get("index") or _new_tool_call_id()
        parts.append(
            ToolCallPart(
                tool_name=function.get("name", "unknown_tool"),
                args=args,
                tool_call_id=str(tool_call_id),
            )
        )

    usage = _map_usage(response_json)
    finish_reason = _map_finish_reason(response_json.get("done_reason"))

    return ModelResponse(
        parts=parts,
        model_name=response_json.get("model") or SUPPORTED_MODEL_NAME,
        usage=usage,
        finish_reason=finish_reason,
    )


def _map_finish_reason(done_reason: str | None) -> FinishReason | None:
    """Map Ollama's ``done_reason`` to Pydantic AI's ``FinishReason``."""
    if done_reason in {"stop", "length", "content_filter", "tool_calls"}:
        return done_reason  # type: ignore[return-value]
    return None


def _map_usage(response_json: dict[str, Any]) -> RequestUsage:
    """Build ``RequestUsage`` from Ollama token counts."""
    return RequestUsage(
        input_tokens=response_json.get("prompt_eval_count") or 0,
        output_tokens=response_json.get("eval_count") or 0,
    )


def _new_tool_call_id() -> str:
    """Generate a fresh tool-call id when the model omits one."""
    import uuid

    return f"ollama_{uuid.uuid4().hex[:12]}"


@dataclass(init=False)
class OllamaCloudNativeModel(Model):
    """Pydantic AI Model adapter for Ollama Cloud's native ``/api/chat`` API.

    Uses exactly ``gpt-oss:120b-cloud`` and a Bearer token normalized from the
    caller. The adapter is intentionally minimal: it supports the message kinds,
    tools, and structured-output mode used by the STA investigator and raises
    clear errors for unsupported capabilities.
    """

    _api_key: str
    _base_url: str
    _http_timeout: float

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = OLLAMA_CLOUD_NATIVE_URL,
        http_timeout: float = 300.0,
        settings: ModelSettings | None = None,
        profile: ModelProfile | None = None,
    ) -> None:
        """Initialize the model.

        Args:
            api_key: Ollama Cloud API key (Bearer token). Treated as a secret;
                never rendered by ``repr``/``str``.
            base_url: Ollama native chat endpoint. Defaults to Ollama Cloud.
            http_timeout: Request timeout in seconds.
            settings: Optional model-specific default settings.
            profile: Optional model profile override. When omitted, a cloud-safe
                profile (tools enabled, native JSON schema disabled) is used.
        """
        if not api_key:
            raise UserError("OllamaCloudNativeModel requires a non-empty api_key")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._http_timeout = http_timeout

        if profile is None:
            # Cloud native accepts ``format: json_schema`` without error but does
            # not enforce grammar-constrained decoding, so force Pydantic AI to
            # use ToolOutput for structured reports.
            profile = merge_profile(
                DEFAULT_PROFILE,
                ModelProfile(
                    supports_json_schema_output=False,
                    default_structured_output_mode="tool",
                ),
            )

        super().__init__(settings=settings, profile=profile)

    @property
    def model_name(self) -> str:
        return SUPPORTED_MODEL_NAME

    @property
    def system(self) -> str:
        return "ollama"

    @property
    def base_url(self) -> str | None:
        return self._base_url

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(model_name={self.model_name!r}, base_url={self.base_url!r})"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        """Make a single non-streaming request to Ollama's native ``/api/chat``."""
        model_settings, params = self.prepare_request(model_settings, model_request_parameters)

        tool_defs = [*params.declared_function_tools, *params.output_tools]
        ollama_messages = _map_messages(messages, params)
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": ollama_messages,
            "stream": False,
        }
        if tool_defs:
            payload["tools"] = [_map_tool_definition(t) for t in tool_defs]

        if model_settings:
            # Forward temperature if provided; Ollama nests runtime options under
            # the ``options`` key.
            options: dict[str, Any] = {}
            if "temperature" in model_settings:
                options["temperature"] = model_settings["temperature"]
            if "top_p" in model_settings:
                options["top_p"] = model_settings["top_p"]
            if options:
                payload["options"] = options

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self._http_timeout) as client:
                response = await client.post(self._base_url, headers=headers, json=payload)
                response.raise_for_status()
                response_json = response.json()
        except httpx.HTTPStatusError as exc:
            body: Any = None
            try:
                body = exc.response.json()
            except Exception:
                body = exc.response.text
            raise ModelHTTPError(
                status_code=exc.response.status_code,
                model_name=self.model_name,
                body=body,
                headers=dict(exc.response.headers),
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelHTTPError(
                status_code=0,
                model_name=self.model_name,
                body=str(exc),
                headers={},
            ) from exc

        return _parse_response(response_json)

    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> Any:
        """Streamed requests are not supported by this adapter."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support streamed requests"
        )

    async def count_tokens(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> RequestUsage:
        """Token counting ahead of the request is not supported."""
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support count_tokens_before_request"
        )
