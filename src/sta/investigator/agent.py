"""Investigator orchestration seam (Architecture.md #22-#23, #29-#30, #34-#35).

The Investigator is the only component that interprets. This module owns the
seam between STA's deterministic surface and the model:

- :class:`InvestigationTools` — the complete model-facing tool surface:
  validated query-tool calls through the QueryRunner, stored-result read/list,
  and bounded knowledge search/read. No SQL, no diagnosis, no recommendation
  logic exists here or anywhere in STA.
- :class:`PydanticAiInvestigator` — the Pydantic AI implementation: strong
  system prompt, deterministic tools, structured report output and report
  reference validation with model-retry feedback.
- :class:`CallbackInvestigator` — a conservative fallback that runs an
  *explicitly supplied* callback producing the report. It exists for tests and
  offline report replay; it is not a diagnosis engine and is never a default.
- :func:`create_investigator` — builds the investigator with explicit
  precedence (model argument, callback, ``STA_INVESTIGATOR_MODEL`` override)
  and then falls back to the settings-configured Ollama investigator: exactly
  ``gpt-oss:120b-cloud`` against Ollama Cloud's native ``/api/chat`` endpoint
  by default, or Pydantic AI's bundled OpenAI-compatible ``OllamaModel``
  pinned to the local ``gpt-oss:120b`` tag when an explicit
  ``OLLAMA_BASE_URL`` (local daemon) is configured. The API key is
  normalized from ``OLLAMA_API_KEY``/legacy ``LOCAL_OLAMMA_API_KEY`` by
  :mod:`sta.config` and never exposed. Fails closed when no model and no
  explicit callback is configured.

The model owns interpretation; everything here is deterministic plumbing.
"""

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.usage import UsageLimits

from sta.config import OLLAMA_CLOUD_BASE_URL, OLLAMA_CLOUD_NATIVE_URL, get_settings
from sta.execution.errors import ToolExecutionError
from sta.investigator.ollama_cloud_native_model import OllamaCloudNativeModel
from sta.investigator.prompt import build_system_prompt, build_user_prompt
from sta.investigator.report import (
    KNOWLEDGE_READ_EVENT,
    ReportReferenceValidator,
    ReportValidationError,
)
from sta.knowledge.repository import KnowledgeBase
from sta.knowledge.read import KnowledgeAccessError
from sta.results.models import QueryResult
from sta.results.store import FULL_SCHEMA_RESULT_ID
from sta.tools.registry import DEFAULT_REGISTRY
from sta.tools.spec import ToolSpec

INVESTIGATOR_TOOL_NAMES = (
    "run_query",
    "list_results",
    "read_result",
    "search_knowledge",
    "read_knowledge",
)

# Bounds that keep tool output inside the model context (Architecture.md #34).
MAX_TOOL_RESULT_ROWS = 25
MAX_READ_ROWS_DEFAULT = 50
MAX_READ_ROWS_LIMIT = 200

MODEL_ENV_VAR = "STA_INVESTIGATOR_MODEL"

# The pinned investigator model for the Ollama Cloud default: the cloud-hosted
# ``gpt-oss:120b-cloud`` tag served through the native ``/api/chat`` adapter.
# The model name is pinned; only the endpoint is configurable
# (OLLAMA_BASE_URL), so no fallback provider or model can be selected through
# configuration.
OLLAMA_MODEL_NAME = "gpt-oss:120b-cloud"

# Explicit local daemon (OLLAMA_BASE_URL set) only. Naming differs by design:
# ``-cloud`` is an Ollama-Cloud-hosted tag that does not exist on a local
# daemon, so the local path keeps Ollama's local tag ``gpt-oss:120b`` — the
# same Ollama gpt-oss 120b model, never another vendor or model.
OLLAMA_LOCAL_MODEL_NAME = "gpt-oss:120b"


class InvestigatorNotConfiguredError(Exception):
    """No Pydantic AI model and no explicit fallback callback is configured.

    STA fails closed: there is no silent diagnosis fallback."""


class ReportRejectedError(Exception):
    """The produced report failed deterministic validation after retries.

    Carries only a sanitized, deterministic message and safe validator
    details. The raw model output is never stored here.
    """

    def __init__(self, message: str, *, safe_details: dict[str, Any] | None = None):
        self.safe_details = safe_details or {}
        super().__init__(message)


class ModelTransportError(Exception):
    """The investigator model request failed at the transport boundary.

    Typed, safe normalization of Pydantic AI model-transport failures
    (``ModelHTTPError`` and network-level ``httpx`` errors): the run fails
    clearly instead of escaping as an untyped exception that the lifecycle can
    only record as a generic internal error — together with the provider
    response body.

    Safe by construction: the message and ``safe_details`` carry only coarse
    classification (a stable reason and, for HTTP failures, the numeric status
    code). They never contain the response body, the API key, the endpoint,
    or raw model output. The original exception stays on ``__cause__`` for
    in-process diagnosis only; it is never rendered into events or logs.
    """

    def __init__(self, message: str, *, safe_details: dict[str, Any] | None = None):
        self.safe_details = safe_details or {}
        super().__init__(message)


class ResultAccess(Protocol):
    """Read/event seam over stored run results (satisfied by ResultStore).

    ``list_events`` exposes the run's persisted safe events; the report
    validator derives the knowledge paths actually read in this run from them.
    """

    def list_results(self, run_id: str) -> list[QueryResult]: ...

    def get_result(self, run_id: str, result_id: str) -> QueryResult | None: ...

    def append_event(self, run_id: str, event_type: str, data: dict[str, Any] | None = None) -> Any: ...

    def list_events(self, run_id: str) -> list[Any]: ...


class QueryRunnerLike(Protocol):
    """Execution seam (satisfied by QueryRunner)."""

    def run(self, tool_name: str, parameters: Any = None) -> Any: ...


@dataclass
class InvestigationSession:
    """Everything one investigation may touch: fixed run identity, the query
    runner, stored results, and the knowledge corpus. Nothing else."""

    run_id: str
    table: str
    snapshot_id: str | None
    table_context: BaseModel
    runner: Any
    store: ResultAccess
    knowledge: KnowledgeBase
    event_sink: Callable[[str, dict[str, Any]], None] | None = None

    def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        sink = self.event_sink or self._store_event_sink()
        if sink is not None:
            sink(event_type, data)

    def _store_event_sink(self) -> Callable[[str, dict[str, Any]], None] | None:
        append = getattr(self.store, "append_event", None)
        if append is None:
            return None
        return lambda event_type, data: append(self.run_id, event_type, data)


class Investigator(Protocol):
    """The explicit investigator seam. Implementations are either the Pydantic
    AI agent or an explicitly injected test/report callback."""

    def investigate(self, session: InvestigationSession) -> BaseModel:
        """Run the investigation and return a validated report."""
        ...


class InvestigationTools:
    """The deterministic model-facing tool surface.

    Exactly five tools, all deterministic:

    - ``run_query``: one validated predefined query via QueryRunner,
    - ``list_results``: the run's stored-result index,
    - ``read_result``: a stored result with row pagination,
    - ``search_knowledge``: bounded lexical knowledge search,
    - ``read_knowledge``: bounded knowledge document range.

    Knowledge tools carry an evidence-context guard (Architecture.md #20:
    knowledge informs measurements, it is not table evidence): until the run
    has its first stored measurement (R001+), they return a typed safe
    precondition error instead of content, so the investigation starts with
    measurements. After one measurement, knowledge is available normally.

    Failures return typed error dicts (never SQL text); successes return
    bounded payloads. Nothing interprets values.
    """

    def __init__(self, session: InvestigationSession, registry: dict[str, ToolSpec] | None = None):
        self._session = session
        self._registry = registry if registry is not None else DEFAULT_REGISTRY

    # -- query tools (predefined queries only, via QueryRunner) --------------

    def run_query(self, tool_name: str, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
        """Execute one reviewed predefined query; every success is stored as Rxxx."""
        try:
            outcome = self._session.runner.run(tool_name, parameters)
        except ToolExecutionError as exc:
            return _tool_error(exc.error_class, exc.message, retryable=exc.retryable)
        spec = self._registry.get(tool_name)
        return {
            "result_id": outcome.result_id,
            "tool_name": outcome.tool_name,
            "query_version": outcome.query_version,
            "snapshot_id": outcome.snapshot_id,
            "row_count": outcome.query_result.row_count,
            "payload": _bounded_payload(spec, outcome.payload),
        }

    # -- stored results ------------------------------------------------------

    def list_results(self) -> list[dict[str, Any]]:
        """The small stored-result index for this run (Architecture.md #18)."""
        return [
            {
                "result_id": result.result_id,
                "tool_name": result.tool_name,
                "query_version": result.query_version,
                "snapshot_id": result.snapshot_id,
                "row_count": result.row_count,
                "executed_at": result.executed_at,
            }
            for result in self._session.store.list_results(self._session.run_id)
        ]

    def read_result(
        self, result_id: str, start_row: int = 1, max_rows: int = MAX_READ_ROWS_DEFAULT
    ) -> dict[str, Any]:
        """Read one stored result, paginated for tabular payloads."""
        if not isinstance(result_id, str) or not re.fullmatch(r"R\d{3,}", result_id):
            return _tool_error("invalid_result_reference", f"malformed result id {result_id!r}")
        if not isinstance(start_row, int) or start_row < 1:
            return _tool_error("invalid_result_reference", "start_row must be a positive integer")
        if not isinstance(max_rows, int) or max_rows < 1 or max_rows > MAX_READ_ROWS_LIMIT:
            return _tool_error(
                "invalid_result_reference",
                f"max_rows must be between 1 and {MAX_READ_ROWS_LIMIT}",
            )
        result = self._session.store.get_result(self._session.run_id, result_id)
        if result is None:
            return _tool_error(
                "unknown_result_reference",
                f"no result {result_id!r} stored in this run",
            )
        payload, rows_returned, total_rows, truncated = _paged_payload(
            self._registry.get(result.tool_name), result, start_row, max_rows
        )
        return {
            "result_id": result.result_id,
            "tool_name": result.tool_name,
            "query_version": result.query_version,
            "snapshot_id": result.snapshot_id,
            "parameters": result.parameters,
            "schema": result.schema,
            "row_count": result.row_count,
            "payload": payload,
            "start_row": start_row,
            "rows_returned": rows_returned,
            "total_rows": total_rows,
            "truncated": truncated,
        }

    # -- knowledge -------------------------------------------------------------

    def _knowledge_precondition_error(self) -> dict[str, Any] | None:
        """Evidence-context guard for the knowledge tools.

        Knowledge is context for interpreting stored measurements, never a
        substitute for them (Architecture.md #20). While the run holds no
        stored measurement (R001+), the knowledge tools return a typed safe
        precondition error directing the Investigator to its first
        measurement instead. R000, the run-scoped full-schema pseudo-result,
        is structure, not a measurement. This guard changes only when
        knowledge is served; it never judges content, diagnoses, or
        recommends, and once any R001+ result exists it is permanently off.
        """
        stored = self._session.store.list_results(self._session.run_id)
        if any(result.result_id != FULL_SCHEMA_RESULT_ID for result in stored):
            return None
        return _tool_error(
            "no_measurement_evidence",
            "knowledge is context for interpreting measurements, so it stays "
            "unavailable until this run has at least one stored measurement "
            "(R001+); call run_query(tool_name='get_file_layout', parameters={}) "
            "first (or another measurement tool), then use knowledge to "
            "interpret the stored results",
            retryable=True,
        )

    def search_knowledge(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Bounded lexical search over the curated knowledge corpus."""
        precondition = self._knowledge_precondition_error()
        if precondition is not None:
            return precondition
        try:
            hits = self._session.knowledge.search(query, limit=limit)
        except (KnowledgeAccessError, ValueError) as exc:
            return _tool_error("knowledge_access", str(exc))
        self._session.emit_event(
            "knowledge_search_completed",
            {"query": query, "matches": [hit.path for hit in hits]},
        )
        return {
            "query": query,
            "hits": [hit.model_dump(mode="json") for hit in hits],
        }

    def read_knowledge(
        self, path: str, start_line: int = 1, end_line: int | None = None
    ) -> dict[str, Any]:
        """Read one bounded line range of a curated knowledge document."""
        precondition = self._knowledge_precondition_error()
        if precondition is not None:
            return precondition
        try:
            document = self._session.knowledge.read(path, start_line=start_line, end_line=end_line)
        except KnowledgeAccessError as exc:
            return _tool_error("knowledge_access", str(exc))
        self._session.emit_event(KNOWLEDGE_READ_EVENT, {"path": document.path})
        return document.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Pydantic AI implementation
# ---------------------------------------------------------------------------


class InvestigationDeps:
    """Per-run dependencies handed to every tool call and output validator."""

    def __init__(self, tools: InvestigationTools, references: ReportReferenceValidator):
        self.tools = tools
        self.references = references


class PydanticAiInvestigator:
    """Pydantic AI Investigator: system prompt + deterministic tools +
    structured report output, validated against run references with model
    retry on deterministic violations (Architecture.md #22, #32)."""

    def __init__(self, model: Any, *, retries: int = 2):
        from pydantic_ai import Agent, RunContext  # imported lazily: optional dependency at runtime

        from sta.investigator.report import InvestigationReport

        self._model = model
        self._report_type = InvestigationReport
        agent = Agent(
            model,
            name="sta-investigator",
            instructions=build_system_prompt(),
            output_type=InvestigationReport,
            deps_type=InvestigationDeps,
            retries=retries,
        )
        self._agent = agent

        @agent.tool
        def run_query(
            ctx: RunContext[InvestigationDeps],
            tool_name: Annotated[
                str,
                Field(
                    description=(
                        "Name of the reviewed predefined measurement tool to run. "
                        "Examples: get_file_layout, get_partition_layout, "
                        "get_snapshot_history, get_manifest_stats, "
                        "get_delete_file_stats, get_column_metadata_metrics."
                    )
                ),
            ],
            parameters: Annotated[
                dict[str, Any] | None,
                Field(
                    description=(
                        "Tool-specific parameters as a JSON object; use {} or omit "
                        "for tools that take no parameters."
                    )
                ),
            ] = None,
        ) -> dict[str, Any]:
            """Run one reviewed predefined query tool; the result is stored as Rxxx."""
            return ctx.deps.tools.run_query(tool_name, parameters)

        @agent.tool
        def list_results(ctx: RunContext[InvestigationDeps]) -> list[dict[str, Any]]:
            """Index of stored results for this run (Rxxx ids, tools, row counts)."""
            return ctx.deps.tools.list_results()

        @agent.tool
        def read_result(
            ctx: RunContext[InvestigationDeps],
            result_id: str,
            start_row: int = 1,
            max_rows: int = MAX_READ_ROWS_DEFAULT,
        ) -> dict[str, Any]:
            """Read one stored result by Rxxx id; tabular payloads are paginated."""
            return ctx.deps.tools.read_result(result_id, start_row=start_row, max_rows=max_rows)

        @agent.tool
        def search_knowledge(ctx: RunContext[InvestigationDeps], query: str, limit: int = 5) -> dict[str, Any]:
            """Search the curated knowledge corpus; available only after the run's
            first stored measurement (R001+); until then it returns a typed
            precondition error directing you to run_query first."""
            return ctx.deps.tools.search_knowledge(query, limit=limit)

        @agent.tool
        def read_knowledge(
            ctx: RunContext[InvestigationDeps],
            path: str,
            start_line: int = 1,
            end_line: int | None = None,
        ) -> dict[str, Any]:
            """Read a bounded line range of one curated knowledge document;
            available only after the run's first stored measurement (R001+)."""
            return ctx.deps.tools.read_knowledge(path, start_line=start_line, end_line=end_line)

        @agent.output_validator
        def validate_report_references(
            ctx: RunContext[InvestigationDeps], output: InvestigationReport
        ) -> InvestigationReport:
            from pydantic_ai import ModelRetry

            errors = ctx.deps.references.validate(output)
            if errors:
                raise ModelRetry(
                    "the report violates deterministic reference rules; fix and resubmit: "
                    + "; ".join(errors)
                )
            return output

    @property
    def tool_names(self) -> tuple[str, ...]:
        """The fixed model-facing tool surface (no arbitrary SQL tool exists)."""
        return INVESTIGATOR_TOOL_NAMES

    def investigate(self, session: InvestigationSession) -> BaseModel:
        tools = InvestigationTools(session)
        references = _reference_validator(session)
        deps = InvestigationDeps(tools=tools, references=references)
        from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

        try:
            result = self._agent.run_sync(
                build_user_prompt(session),
                deps=deps,
                usage_limits=UsageLimits(request_limit=100),
            )
        except ModelHTTPError as exc:
            # The provider rejected or failed the HTTP request (e.g. 401 for a
            # rejected key). The raw exception carries the response body and
            # headers; only the sanitized classification crosses this seam.
            raise _model_transport_error_from_http_error(exc) from exc
        except httpx.HTTPError as exc:
            # Network-level transport failure (connect/timeout/read). Same
            # safe normalization: no connection details are preserved.
            raise _model_transport_error_from_transport_error(exc) from exc
        except UnexpectedModelBehavior as exc:
            # The model (or its output path) did not honor the report contract.
            # Keep the message deterministic and free of raw response text.
            raise ReportRejectedError(
                "model output did not conform to the report contract",
                safe_details={
                    "reason": "unexpected_model_behavior",
                    "exception_type": type(exc).__name__,
                },
            ) from exc
        except ValidationError as exc:
            raise _report_rejected_from_validation_error(exc) from exc

        report = result.output
        errors = references.validate(report)
        if errors:
            raise _report_rejected_from_errors(errors)
        return report


class CallbackInvestigator:
    """Conservative fallback driven by an explicitly supplied callback.

    The callback is injected by the caller (tests, offline report replay) and
    produces the report; this class adds only parsing and deterministic
    validation. It is NOT a diagnosis engine and never used as one.
    """

    def __init__(self, callback: Callable[[InvestigationSession], Any]):
        self._callback = callback

    @property
    def callback(self) -> Callable[[InvestigationSession], Any]:
        return self._callback

    def investigate(self, session: InvestigationSession) -> BaseModel:
        raw = self._callback(session)
        validator = _reference_validator(session)
        try:
            report = validator.require_valid(raw)
        except ReportValidationError as exc:
            raise _report_rejected_from_errors(exc.errors) from exc
        return report


def _configured_ollama_model() -> Any | None:
    """Build the settings-configured Ollama model, or ``None`` without a key.

    Endpoint selection:

    - Ollama Cloud (no explicit ``OLLAMA_BASE_URL``): the custom native
      ``/api/chat`` adapter, model pinned to ``gpt-oss:120b-cloud``, against
      ``https://ollama.com/api/chat``. The API key is normalized by
      :mod:`sta.config` and handed only to the request header.
    - Local Ollama daemon (``OLLAMA_BASE_URL`` is set explicitly): Pydantic AI's
      bundled ``OllamaModel`` against the provided OpenAI-compatible base URL.
      Naming differs by design: ``-cloud`` is an Ollama-Cloud-hosted tag, so
      the local daemon keeps the local tag ``gpt-oss:120b`` — the same Ollama
      gpt-oss 120b model, never another vendor or model.

    No fallback provider or model exists: without a key this returns ``None``
    and the caller fails closed.
    """
    settings = get_settings()
    if not settings.ollama_api_key:
        return None

    # Imported lazily like the rest of the pydantic_ai surface (optional
    # dependency at runtime).
    from pydantic_ai.models.ollama import OllamaModel
    from pydantic_ai.providers.ollama import OllamaProvider

    if settings.ollama_base_url:
        # Explicit local daemon: keep Pydantic AI's native OpenAI-compatible
        # Ollama provider so local /v1 endpoints keep working unchanged.
        provider = OllamaProvider(
            base_url=settings.ollama_base_url,
            api_key=settings.ollama_api_key,
        )
        return OllamaModel(OLLAMA_LOCAL_MODEL_NAME, provider=provider)

    # Cloud default: native /api/chat adapter, because the supplied Ollama Cloud
    # key succeeds only at the native endpoint, not at the OpenAI-compatible /v1.
    return OllamaCloudNativeModel(api_key=settings.ollama_api_key, base_url=OLLAMA_CLOUD_NATIVE_URL)


def create_investigator(
    *,
    model: Any | None = None,
    callback: Callable[[InvestigationSession], Any] | None = None,
) -> Investigator:
    """Build the investigator. Precedence: explicit model argument, explicit
    callback, the ``STA_INVESTIGATOR_MODEL`` override, then the configured
    Ollama investigator (cloud default: exactly ``gpt-oss:120b-cloud`` via the
    native ``/api/chat`` adapter; an explicit local ``OLLAMA_BASE_URL`` keeps
    Pydantic AI's bundled OllamaModel with the local ``gpt-oss:120b`` tag;
    enabled automatically by an Ollama API key in the environment or
    ``.env``). Fails closed otherwise."""
    if callback is not None:
        if model is not None:
            raise ValueError("provide either a model or a callback, not both")
        return CallbackInvestigator(callback)
    if model is not None:
        return PydanticAiInvestigator(model)
    configured = os.environ.get(MODEL_ENV_VAR)
    if configured:
        return PydanticAiInvestigator(configured)
    ollama_model = _configured_ollama_model()
    if ollama_model is not None:
        return PydanticAiInvestigator(ollama_model)
    raise InvestigatorNotConfiguredError(
        "no investigator configured: set a Pydantic AI model (argument or "
        f"{MODEL_ENV_VAR}), configure the Ollama investigator with an API key "
        "(OLLAMA_API_KEY or the legacy LOCAL_OLAMMA_API_KEY in the environment "
        "or .env), or supply an explicit test/report callback"
    )


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _model_transport_error_from_http_error(exc: Any) -> "ModelTransportError":
    """Normalize a Pydantic AI ``ModelHTTPError`` into a typed, safe failure.

    Only coarse, secret-free facts survive: a stable reason and the numeric
    status code. The provider response body, headers, endpoint and any
    credential material never cross this boundary.
    """
    status_code = exc.status_code
    if status_code in (401, 403):
        return ModelTransportError(
            "investigator model request failed: authentication/configuration "
            f"error (HTTP {status_code}); verify the configured model credentials",
            safe_details={
                "reason": "model_authentication_failed",
                "status_code": status_code,
            },
        )
    if status_code == 0:
        # The native adapter maps network-level httpx errors to status 0.
        return ModelTransportError(
            "investigator model request failed: the model service could not be "
            "reached; verify the network and model configuration",
            safe_details={"reason": "model_unreachable"},
        )
    return ModelTransportError(
        f"investigator model request failed with HTTP status {status_code}",
        safe_details={"reason": "model_http_error", "status_code": status_code},
    )


def _model_transport_error_from_transport_error(exc: Any) -> "ModelTransportError":
    """Normalize a network-level transport error (``httpx``) into a typed,
    safe model failure; no connection details are preserved."""
    return ModelTransportError(
        "investigator model request failed: the model service could not be "
        "reached; verify the network and model configuration",
        safe_details={
            "reason": "model_unreachable",
            "exception_type": type(exc).__name__,
        },
    )


def _reference_validator(session: InvestigationSession) -> ReportReferenceValidator:
    return ReportReferenceValidator(
        store=session.store,
        run_id=session.run_id,
        table=session.table,
        snapshot_id=session.snapshot_id,
        knowledge=session.knowledge,
    )


def _report_rejected_from_errors(errors: list[str]) -> ReportRejectedError:
    """Build a deterministic, sanitized ReportRejectedError from validator errors.

    The message and safe details contain only the deterministic validator
    output; the raw model response is never included.
    """
    total = len(errors)
    first = errors[0]
    message = f"report rejected: {total} deterministic validation error(s); first: {first}"
    safe_details: dict[str, Any] = {
        "validation_error_count": total,
        "first_error": first,
        "error_categories": _error_categories(errors),
        "errors": errors[:5] if total > 1 else errors,
    }
    return ReportRejectedError(message, safe_details=safe_details)


def _report_rejected_from_validation_error(exc: ValidationError) -> ReportRejectedError:
    """Convert a Pydantic validation error into a safe ReportRejectedError.

    Strips any input values from the error details so the event/logs never
    carry raw model output.
    """
    safe_errors: list[str] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        message = error.get("msg", "invalid value")
        safe_errors.append(f"{location}: {message}")
    return _report_rejected_from_errors(safe_errors)


def _error_categories(errors: list[str]) -> list[str]:
    """Stable, coarse buckets for validator errors (used only in safe event/logs)."""
    categories: set[str] = set()
    for error in errors:
        lower = error.lower()
        if "schema" in lower or "does not match" in lower:
            categories.add("schema")
        elif "result reference" in lower:
            categories.add("reference")
        elif "knowledge" in lower:
            categories.add("knowledge")
        elif "metadata" in lower:
            categories.add("metadata")
        elif "table" in lower or "snapshot" in lower:
            categories.add("identity")
        elif "measurement" in lower:
            categories.add("measurement")
        else:
            categories.add("other")
    return sorted(categories)


def _tool_error(error_class: str, message: str, *, retryable: bool = False) -> dict[str, Any]:
    # Typed, safe failure surface (Architecture.md #35): no SQL, no
    # connection details, no engine output.
    return {"error": True, "error_class": error_class, "message": message, "retryable": retryable}


def _bounded_payload(spec: ToolSpec | None, payload: BaseModel) -> dict[str, Any] | None:
    """Bound tool payloads: long tabular lists are truncated to a preview."""
    if payload is None:
        return None
    data = payload.model_dump(mode="json")
    rows_field = spec.rows_field if spec is not None else None
    if rows_field and isinstance(data.get(rows_field), list):
        rows = data[rows_field]
        if len(rows) > MAX_TOOL_RESULT_ROWS:
            data[rows_field] = rows[:MAX_TOOL_RESULT_ROWS]
            data["rows_truncated"] = True
            data["note"] = (
                f"showing first {MAX_TOOL_RESULT_ROWS} of {len(rows)} rows; "
                "use read_result for the full stored result"
            )
    return data


def _paged_payload(
    spec: ToolSpec | None, result: QueryResult, start_row: int, max_rows: int
) -> tuple[Any, int | None, int | None, bool]:
    """Slice a tabular stored payload by rows; summary payloads pass through."""
    data = result.payload if isinstance(result.payload, dict) else None
    if data is None:
        return result.payload, None, None, False
    rows_field = spec.rows_field if spec is not None else None
    rows = data.get(rows_field) if rows_field else None
    if not isinstance(rows, list):
        return data, None, None, False
    total = len(rows)
    begin = min(start_row - 1, total)
    end = min(begin + max_rows, total)
    page = rows[begin:end]
    sliced = dict(data)
    sliced[rows_field] = page
    return sliced, len(page), total, end < total