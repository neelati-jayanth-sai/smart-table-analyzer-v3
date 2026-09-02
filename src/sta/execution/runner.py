"""Shared QueryRunner (Architecture.md #12, #15, #17, #35).

All query tools execute through this one layer:

    validate parameters -> check backend support -> apply table/snapshot scope
    -> execute reviewed query on the active backend (with timeout and one
    deterministic retry for transient failures) -> build the shared result
    contract -> persist every successful execution via ResultStore as R001+
    -> return reference + typed payload.

The runner measures and remembers. It never interprets result values, never
diagnoses, and never sees SQL: queries are reviewed templates or fixture
computations inside the backends.

Snapshot consistency (Architecture.md #15): the runner holds the pinned
snapshot from run start and hands it to every snapshot-scoped tool. A stored
result's ``snapshot_id`` records the snapshot actually measured; ``None``
marks an explicitly not-pinned measurement (non-scoped tools such as
``get_iomete_maintenance_config``).
"""

import logging
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any

from pydantic import BaseModel, ValidationError

from sta.execution.backends.base import BackendExecution, TableBackend
from sta.execution.errors import (
    BackendExecutionError,
    ParameterValidationError,
    QueryTimeoutError,
    ToolExecutionError,
    UnknownToolError,
    UnsupportedToolError,
)
from sta.results.models import QueryResult, utc_now
from sta.tools.spec import ToolSpec

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_ATTEMPTS = 2  # the initial attempt plus one deterministic retry


class ToolOutcome(BaseModel):
    """What a tool call returns to the Investigator: the immutable stored
    reference (Rxxx) plus the typed backend-independent payload."""

    result_id: str
    run_id: str
    tool_name: str
    query_version: str
    snapshot_id: str | None = None
    duration_ms: int
    query_result: QueryResult
    payload: BaseModel


class QueryRunner:
    """One runner per run: fixed table, fixed pinned snapshot, fixed store."""

    def __init__(
        self,
        *,
        backend: TableBackend,
        store: Any,
        run_id: str,
        table: str,
        pinned_snapshot_id: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        registry: Mapping[str, Any] | None = None,
    ) -> None:
        self._backend = backend
        self._store = store
        self._run_id = run_id
        self._table = table
        self._pinned_snapshot_id = pinned_snapshot_id
        self._timeout_seconds = timeout_seconds
        if registry is None:
            from sta.tools.registry import DEFAULT_REGISTRY  # noqa: PLC0415 - avoids an import cycle at module load

            registry = DEFAULT_REGISTRY
        self._registry = registry
        if backend.table != table:
            raise ValueError(
                f"backend table {backend.table!r} does not match run table {table!r}"
            )
        # Two workers: a timed-out attempt keeps running in its thread, and the
        # one deterministic retry must not queue behind it.
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sta-query")

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def table(self) -> str:
        return self._table

    @property
    def pinned_snapshot_id(self) -> str | None:
        return self._pinned_snapshot_id

    def supported_tools(self) -> list[str]:
        return sorted(tool for tool in self._registry if tool in self._backend.supported_tools())

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "QueryRunner":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- execution ----------------------------------------------------------

    def run(self, tool_name: str, parameters: Mapping[str, Any] | BaseModel | None = None) -> ToolOutcome:
        """Validate, execute, persist and return one tool measurement.

        Failures raise a typed ToolExecutionError and persist nothing
        (Architecture.md #35).
        """
        spec = self._registry.get(tool_name)
        if spec is None:
            raise UnknownToolError(tool_name)
        if tool_name not in self._backend.supported_tools():
            raise UnsupportedToolError(tool_name, self._backend.name)

        try:
            validated = self._validate_parameters(spec, parameters)
        except ParameterValidationError as exc:
            # A rejected call is still observable run progress: it is shown as
            # requested + failed, with no parameters echoed back.
            self._event("tool_requested", {"tool": tool_name})
            self._event("tool_failed", _failure_data(exc))
            raise
        parameters_dict = validated.model_dump(mode="json")
        self._event("tool_requested", {"tool": tool_name, "parameters": parameters_dict})
        self._event(
            "query_started",
            {"tool": tool_name, "query_version": spec.query_version},
        )

        started = time.perf_counter()
        try:
            execution = self._execute_with_one_retry(tool_name, parameters_dict)
            duration_ms = int((time.perf_counter() - started) * 1000)
            payload = self._build_payload(spec, execution, validated)
            snapshot_id = self._measurement_snapshot(spec, execution)
        except ToolExecutionError as exc:
            self._event("tool_failed", _failure_data(exc))
            logger.warning(
                "run=%s tool=%s status=failed error_class=%s",
                self._run_id, tool_name, exc.error_class,
            )
            raise

        result = QueryResult(
            run_id=self._run_id,
            tool_name=tool_name,
            query_version=spec.query_version,
            table=self._table,
            snapshot_id=snapshot_id,
            parameters=parameters_dict,
            schema=spec.payload_schema(),
            row_count=spec.row_count(payload),
            payload=payload.model_dump(mode="json"),
            duration_ms=duration_ms,
            executed_at=utc_now(),
        )
        result_id = self._store.store_result(result)
        self._event(
            "result_stored",
            {
                "tool": tool_name,
                "result_id": result_id,
                "row_count": result.row_count,
                "duration_ms": duration_ms,
            },
        )
        logger.info(
            "run=%s tool=%s result=%s duration_ms=%d status=ok",
            self._run_id, tool_name, result_id, duration_ms,
        )
        return ToolOutcome(
            result_id=result_id,
            run_id=self._run_id,
            tool_name=tool_name,
            query_version=spec.query_version,
            snapshot_id=snapshot_id,
            duration_ms=duration_ms,
            query_result=result,
            payload=payload,
        )

    def _measurement_snapshot(self, spec: ToolSpec, execution: BackendExecution) -> str | None:
        """Snapshot scope recorded on the stored result (Architecture.md #15).

        Non-scoped tools store ``None`` — the explicit 'not pinned' mark.
        Snapshot-scoped tools must be measured at this run's pinned snapshot:
        a backend that reports no snapshot (e.g. an empty table has none) or
        a different one fails closed instead of storing misattributed or
        fabricated evidence (never snapshot 0)."""
        if not spec.snapshot_scoped:
            return None
        if execution.snapshot_id is None:
            raise SnapshotNotAvailableError(
                spec.name,
                "the backend measured no snapshot for this snapshot-scoped tool "
                "(a table without snapshots cannot serve it)",
            )
        if (
            self._pinned_snapshot_id is not None
            and execution.snapshot_id != self._pinned_snapshot_id
        ):
            raise SnapshotNotAvailableError(
                spec.name,
                f"measured snapshot {execution.snapshot_id!r} does not match this "
                f"run's pinned snapshot {self._pinned_snapshot_id!r}",
            )
        return execution.snapshot_id

    def _validate_parameters(self, spec: ToolSpec, parameters) -> BaseModel:
        if parameters is None:
            return spec.parameters.model_validate({})
        if isinstance(parameters, spec.parameters):
            return parameters  # already validated at construction
        if isinstance(parameters, BaseModel):
            parameters = parameters.model_dump(mode="json")
        if not isinstance(parameters, Mapping):
            raise ParameterValidationError(
                spec.name, f"invalid {spec.name} parameters: expected an object"
            )
        try:
            return spec.parameters.model_validate(dict(parameters))
        except ValidationError as exc:
            raise ParameterValidationError(
                spec.name, _safe_validation_message(spec, exc)
            ) from exc

    def _execute_with_one_retry(self, tool_name: str, parameters: Mapping[str, Any]) -> BackendExecution:
        last_error: ToolExecutionError | None = None
        for attempt in range(_MAX_ATTEMPTS):
            if attempt:
                logger.info(
                    "run=%s tool=%s status=retry attempt=%d",
                    self._run_id, tool_name, attempt + 1,
                )
            try:
                return self._execute_once(tool_name, parameters)
            except ToolExecutionError as exc:
                last_error = exc
                if not exc.retryable:
                    raise
        raise last_error if last_error is not None else AssertionError("unreachable")

    def _execute_once(self, tool_name: str, parameters: Mapping[str, Any]) -> BackendExecution:
        """Run the backend with the configured timeout enforced."""
        future = self._pool.submit(self._backend.execute, tool_name, dict(parameters), self._pinned_snapshot_id)
        try:
            return future.result(timeout=self._timeout_seconds)
        except FutureTimeoutError:
            future.cancel()
            raise QueryTimeoutError(tool_name, self._timeout_seconds) from None
        except ToolExecutionError:
            raise
        except Exception as exc:  # unexpected backend failure: typed, safe message
            raise BackendExecutionError(
                tool_name, f"unexpected backend error ({type(exc).__name__})"
            ) from exc

    def _build_payload(self, spec, execution: BackendExecution, validated) -> BaseModel:
        try:
            return spec.build_payload(execution.rows, validated)
        except ToolExecutionError:
            raise
        except Exception as exc:  # contract mismatch is an implementation bug
            raise BackendExecutionError(
                spec.name, "result normalization failed for the tool contract"
            ) from exc

    def _event(self, event_type: str, data: dict[str, Any]) -> None:
        self._store.append_event(self._run_id, event_type, data)


def _failure_data(exc: ToolExecutionError) -> dict[str, Any]:
    return {
        "tool": exc.tool_name,
        "error_class": exc.error_class,
        "retryable": exc.retryable,
    }


def _safe_validation_message(spec: ToolSpec, exc: ValidationError) -> str:
    """First validation error, rendered from validated-model context only
    (parameter names/values the model already exposes — never engine output)."""
    errors = exc.errors()
    if not errors:
        return "invalid parameters"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = first.get("msg", "invalid value")
    return f"invalid {spec.name} parameters: {location}: {message}"