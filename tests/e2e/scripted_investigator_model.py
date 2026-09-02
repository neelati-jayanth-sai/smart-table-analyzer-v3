"""Deterministic scripted stand-in for the investigator LLM (pydantic-ai 2.37).

pydantic-ai 2.37 ships ``TestModel`` but no longer ships ``FunctionModel``, and
``TestModel`` cannot script a *sequence* of realistic investigator steps (it
calls every tool once with schema-generated arguments). This module supplies
the missing test seam: a :class:`pydantic_ai.models.Model` subclass whose every
request step is answered by a test-supplied script function, so the real
:class:`~sta.investigator.agent.PydanticAiInvestigator` agent loop — tool
calling, output validation, model retry — runs end-to-end with no LLM and no
network.

The script signature mirrors the removed official ``FunctionModel``:
``script(messages, model_request_parameters) -> ModelResponse``.

Test infrastructure only: nothing here is importable from ``sta`` production
code, and no production fallback ever selects a scripted model.
"""

from typing import Callable

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters
from pydantic_ai.settings import ModelSettings

#: ``script(messages, model_request_parameters) -> ModelResponse``
InvestigatorScript = Callable[[list[ModelMessage], ModelRequestParameters], ModelResponse]


class ScriptedInvestigatorModel(Model):
    """Fake Pydantic AI model compatible with pydantic-ai 2.37.

    Every agent request step is answered by calling ``script`` with the full
    message history and the resolved request parameters (function tools and
    output tools included). Fully deterministic: no network, no randomness,
    no LLM. Every request is recorded so tests can assert on the exact
    conversation the real agent produced.
    """

    def __init__(self, script: InvestigatorScript) -> None:
        self.script = script
        self.requests: list[list[ModelMessage]] = []
        super().__init__()

    @property
    def model_name(self) -> str:
        return "scripted-investigator"

    @property
    def system(self) -> str:
        return "test"

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        _, resolved_parameters = self.prepare_request(model_settings, model_request_parameters)
        self.requests.append(list(messages))
        return self.script(list(messages), resolved_parameters)


# ---------------------------------------------------------------------------
# scripting helpers
# ---------------------------------------------------------------------------


def tool_call(name: str, args: dict) -> ModelResponse:
    """Script one tool call."""
    return ModelResponse(parts=[ToolCallPart(name, args, tool_call_id=f"scripted-{name}")])


def final_report_call(model_request_parameters: ModelRequestParameters, report: dict) -> ModelResponse:
    """Script the structured final report on the agent's declared output tool.

    The output tool is read from the resolved request parameters (never
    hardcoded), so the fake model stays compatible across pydantic-ai output
    tool naming.
    """
    output_tool = model_request_parameters.output_tools[0]
    wrapper_key = output_tool.outer_typed_dict_key
    payload = {wrapper_key: report} if wrapper_key else report
    return ModelResponse(
        parts=[ToolCallPart(output_tool.name, payload, tool_call_id="scripted-final")]
    )


def last_tool_returns(messages: list[ModelMessage]) -> list[ToolReturnPart]:
    """Tool results in the newest request — the step the script is answering."""
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            return [part for part in message.parts if isinstance(part, ToolReturnPart)]
    return []


def last_retry_prompts(messages: list[ModelMessage]) -> list[RetryPromptPart]:
    """Retry prompts in the newest request — STA's model-retry feedback."""
    for message in reversed(messages):
        if isinstance(message, ModelRequest):
            return [part for part in message.parts if isinstance(part, RetryPromptPart)]
    return []


def user_prompt_text(messages: list[ModelMessage]) -> str:
    """The run's user prompt text (carries the run identity the report echoes)."""
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    return str(part.content)
    return ""


def instruction_text(model_request_parameters: ModelRequestParameters) -> str:
    """The agent's system instructions exactly as delivered to the model.

    Pydantic AI passes the agent's instructions as instruction parts on the
    resolved request parameters, not as messages; this exposes them so tests
    can assert the model actually received the prompt rules.
    """
    parts = model_request_parameters.instruction_parts or []
    return "\n".join(part.content for part in parts)