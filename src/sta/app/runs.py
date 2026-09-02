"""Run lifecycle service (Runtime_Environments_UI.md #18-#21, #58; Architecture.md #6-#7, #15).

MVP concurrency model: one FastAPI process, one in-process async task per
investigation, SQLite persistence (Runtime_Environments_UI.md #20). The
service is fully dependency-injected through :class:`RunComponents`:

- ``store``            — the ResultStore (runs, Rxxx results, reports, events),
- ``knowledge``        — the curated KnowledgeBase,
- ``metadata_provider``— resolves the user table name into backend-independent
  ``TableMetadata`` (the ``TableMetadataProvider`` seam),
- ``backend_factory``  — builds the run's ``TableBackend`` from the resolved
  metadata,
- ``investigator_factory`` — builds the Investigator (Pydantic AI agent or an
  explicit test/report callback).

Per run, at start (Runtime_Environments_UI.md #19):

1. resolve the table through the metadata provider,
2. build the compact startup context (metadata-derived, no raw DDL),
3. pin the snapshot on the run record,
4. persist the full structural schema as the reserved pseudo-result R000,
5. invoke the Investigator over an InvestigationSession (QueryRunner, store,
   knowledge), validate and store the structured report,
6. update run states/phases robustly and append safe operational events only
   — never model reasoning, secrets or SQL.

Cancellation (Runtime_Environments_UI.md #58) stops future investigator turns
and queued queries, preserves all stored Rxxx evidence and activity history,
and never deletes anything.
"""

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable

from sta.context.context_builder import build_startup_context
from sta.context.table_metadata import TableMetadata, TableMetadataProvider
from sta.execution.backends.base import TableBackend
from sta.execution.errors import (
    BackendNotConfiguredError,
    TableNotResolvedError,
    ToolExecutionError,
)
from sta.execution.runner import QueryRunner
from sta.investigator.agent import (
    InvestigationSession,
    Investigator,
    InvestigatorNotConfiguredError,
    ModelTransportError,
    ReportRejectedError,
    ReportValidationError,
    _error_categories,
)
from sta.investigator.report import ReportReferenceValidator
from sta.knowledge.repository import KnowledgeBase
from sta.results.models import RunRecord, new_run_id, utc_now
from sta.results.store import ResultStore

logger = logging.getLogger(__name__)

# Run-state model (Runtime_Environments_UI.md #21).
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# R000 identity as surfaced in the API/UI and the stored result.
FULL_SCHEMA_TOOL_NAME = "full_schema"
FULL_SCHEMA_QUERY_VERSION = "startup_context:v1"

_TERMINAL_EVENT_TYPES = {
    "completed": "run_completed",
    "failed": "run_failed",
    "cancelled": "run_cancelled",
}


class RunCancelledError(Exception):
    """Internal checkpoint signal: cancellation was requested between steps."""


class UnconfiguredMetadataProvider:
    """Default metadata provider before a real environment is wired.

    Fails closed with a typed, safe error: the application boots and serves
    the UI, but runs stop at table resolution with an actionable operational
    message (Runtime_Environments_UI.md #49: fail fast on missing config).
    """

    def __init__(self, environment: str):
        self._environment = environment

    def load_table_metadata(self, table_name: str) -> TableMetadata:
        raise TableNotResolvedError(
            table_name,
            f"no table metadata source is configured for the {self._environment!r} environment",
        )


def unconfigured_backend_factory(environment: str) -> Callable[[str, TableMetadata], TableBackend]:
    """Default backend factory: fails closed with a typed, safe error."""

    def factory(table: str, metadata: TableMetadata) -> TableBackend:
        raise BackendNotConfiguredError(
            "run-backend",
            f"no query backend is configured for the {environment!r} environment",
        )

    return factory


class RunComponents:
    """Injected collaborators of the run lifecycle (the seams the app needs)."""

    def __init__(
        self,
        *,
        store: ResultStore,
        knowledge: Any,
        metadata_provider: TableMetadataProvider,
        backend_factory: Callable[[str, TableMetadata], TableBackend],
        investigator_factory: Callable[[], Investigator],
    ) -> None:
        self.store = store
        self.knowledge = knowledge
        self.metadata_provider = metadata_provider
        self.backend_factory = backend_factory
        self.investigator_factory = investigator_factory


class RunService:
    """Creates, executes, tracks and cancels investigation runs."""

    def __init__(
        self,
        components: RunComponents,
        *,
        max_concurrent_runs: int = 2,
        query_timeout_seconds: float = 30.0,
    ) -> None:
        if max_concurrent_runs < 1:
            raise ValueError("max_concurrent_runs must be >= 1")
        self._components = components
        self._store = components.store
        self._limiter = asyncio.Semaphore(max_concurrent_runs)
        self._query_timeout_seconds = float(query_timeout_seconds)
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._active_runners: dict[str, QueryRunner] = {}

    @property
    def store(self) -> ResultStore:
        return self._store

    # -- public API ---------------------------------------------------------

    def get_run(self, run_id: str) -> RunRecord | None:
        return self._store.get_run(run_id)

    async def start_run(self, table_name: str) -> RunRecord:
        """Create the run record and launch its bounded in-process task."""
        run = self._store.create_run(
            RunRecord(run_id=new_run_id(), table=table_name, status="queued")
        )
        self._cancel_events[run.run_id] = threading.Event()
        task = asyncio.create_task(
            self._execute(run.run_id, table_name), name=f"sta-run-{run.run_id}"
        )
        self._tasks[run.run_id] = task
        task.add_done_callback(lambda _task, run_id=run.run_id: self._forget(run_id))
        return run

    async def wait_for_run(self, run_id: str) -> RunRecord | None:
        """Wait until the run's task finishes; returns the final record."""
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        return self._store.get_run(run_id)

    async def cancel_run(self, run_id: str) -> RunRecord | None:
        """Cancel one active or queued run; idempotent, evidence-preserving."""
        run = self._store.get_run(run_id)
        if run is None:
            return None
        if run.status in TERMINAL_STATUSES:
            return run
        cancel_event = self._cancel_events.get(run_id)
        if cancel_event is not None:
            cancel_event.set()
        runner = self._active_runners.get(run_id)
        if runner is not None:
            # Stops queued queries; the active attempt finishes in its thread.
            runner.close()
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.wait({task})
        else:
            self._transition(run_id, "cancelled")
        return self._store.get_run(run_id)

    async def shutdown(self) -> None:
        """Cancel every active run (used on application shutdown)."""
        for cancel_event in list(self._cancel_events.values()):
            cancel_event.set()
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- task body ----------------------------------------------------------

    async def _execute(self, run_id: str, table_name: str) -> None:
        cancel_event = self._cancel_events.get(run_id) or threading.Event()
        try:
            async with self._limiter:
                self._checkpoint(cancel_event)
                await asyncio.to_thread(self._investigate, run_id, table_name, cancel_event)
                self._checkpoint(cancel_event)
                self._transition(run_id, "completed")
        except asyncio.CancelledError:
            self._transition(run_id, "cancelled")
            raise
        except RunCancelledError:
            self._transition(run_id, "cancelled")
        except ToolExecutionError as exc:
            self._transition(run_id, "failed", error_class=exc.error_class, message=exc.message)
        except InvestigatorNotConfiguredError as exc:
            self._transition(run_id, "failed", error_class="investigator_not_configured", message=str(exc))
        except ReportRejectedError as exc:
            self._emit_report_rejected(run_id, exc)
            self._transition(
                run_id,
                "failed",
                error_class="report_rejected",
                message=str(exc),
                details=exc.safe_details,
            )
        except ReportValidationError as exc:
            # Defense in depth: agents should normalize to ReportRejectedError,
            # but if a raw validator error reaches the run lifecycle, wrap it safely.
            safe_details = {
                "validation_error_count": len(exc.errors),
                "first_error": exc.errors[0] if exc.errors else None,
                "error_categories": _error_categories(exc.errors),
                "errors": exc.errors[:5] if len(exc.errors) > 1 else exc.errors,
            }
            wrapped = ReportRejectedError(
                f"report rejected: {len(exc.errors)} deterministic validation error(s); "
                f"first: {safe_details['first_error']}",
                safe_details=safe_details,
            )
            self._emit_report_rejected(run_id, wrapped)
            self._transition(
                run_id,
                "failed",
                error_class="report_rejected",
                message=str(wrapped),
                details=safe_details,
            )
        except ModelTransportError as exc:
            # Typed, safe model-transport failure (e.g. rejected provider
            # credentials): recorded with its coarse classification only —
            # never the response body, key, endpoint, or raw model output.
            self._emit_model_failed(run_id, exc)
            self._transition(
                run_id,
                "failed",
                error_class="model_failed",
                message=str(exc),
                details=exc.safe_details,
            )
        except Exception:
            logger.exception("run=%s failed with an internal error", run_id)
            self._transition(
                run_id, "failed",
                error_class="internal_error",
                message="internal error during the investigation",
            )

    def _investigate(self, run_id: str, table_name: str, cancel_event: threading.Event) -> None:
        """The synchronous investigation body; runs in a worker thread so the
        event loop keeps streaming SSE while queries and the Investigator run."""
        store = self._store
        self._checkpoint(cancel_event)
        store.update_run(run_id, status="starting", phase="building_context")
        store.append_event(run_id, "run_started", {"table": table_name})
        store.append_event(run_id, "table_resolving", {"table": table_name})

        try:
            metadata = self._components.metadata_provider.load_table_metadata(table_name)
        except KeyError as exc:
            raise TableNotResolvedError(
                table_name, f"table {table_name!r} was not found in the configured metadata source"
            ) from exc
        if metadata.table != table_name:
            # The metadata provider resolves the canonical catalog.namespace.table
            # identity (e.g. a two-part name resolves under the configured
            # catalog); the canonical name is pinned on the run record exactly
            # like the snapshot (Architecture.md #15) so run, results, report
            # and validator all carry one identity.
            store.update_run(run_id, table=metadata.table)
        snapshot_id = None if metadata.current_snapshot_id is None else str(metadata.current_snapshot_id)
        store.append_event(
            run_id, "table_resolved",
            {"table": metadata.table, "snapshot_id": snapshot_id,
             "format_version": metadata.format_version},
        )

        store.append_event(run_id, "table_context_started", {})
        startup = build_startup_context(metadata)
        store.update_run(run_id, snapshot_id=snapshot_id)  # pin (Architecture.md #15)
        store.append_event(run_id, "snapshot_pinned", {"snapshot_id": snapshot_id})
        full_schema_ref = store.store_full_schema(
            run_id, startup.full_schema,
            tool_name=FULL_SCHEMA_TOOL_NAME,
            query_version=FULL_SCHEMA_QUERY_VERSION,
            table=startup.table_context.table,
            snapshot_id=snapshot_id,
            row_count=len(startup.full_schema.fields),
        )
        store.append_event(
            run_id, "table_context_ready",
            {"schema_id": startup.table_context.schema_id,
             "column_count": len(startup.table_context.schema_summary),
             "full_schema_ref": full_schema_ref},
        )

        self._checkpoint(cancel_event)
        store.update_run(run_id, status="running", phase="investigating")
        store.append_event(run_id, "investigator_started", {})
        # One investigator per run: implementations hold per-run state (agent
        # bindings, validators), so concurrent runs never share an instance.
        investigator = self._components.investigator_factory()
        backend = self._components.backend_factory(metadata.table, metadata)
        runner = QueryRunner(
            backend=backend, store=store, run_id=run_id, table=metadata.table,
            pinned_snapshot_id=snapshot_id, timeout_seconds=self._query_timeout_seconds,
        )
        self._active_runners[run_id] = runner
        try:
            session = InvestigationSession(
                run_id=run_id,
                table=metadata.table,
                snapshot_id=snapshot_id,
                table_context=startup.table_context,
                runner=runner,
                store=store,
                knowledge=self._components.knowledge,
            )
            report = investigator.investigate(session)
        finally:
            self._active_runners.pop(run_id, None)
            runner.close()

        self._checkpoint(cancel_event)
        store.update_run(run_id, phase="generating_report")
        store.append_event(run_id, "report_started", {})
        validated = ReportReferenceValidator(
            store=store, run_id=run_id, table=metadata.table,
            snapshot_id=snapshot_id, knowledge=self._components.knowledge,
        ).require_valid(report)
        store.store_report(run_id, validated.model_dump(mode="json"))
        store.append_event(run_id, "report_ready", {"overall_status": validated.overall_status.value})

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _checkpoint(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise RunCancelledError()

    def _transition(
        self,
        run_id: str,
        status: str,
        *,
        error_class: str | None = None,
        message: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> RunRecord | None:
        """Robust terminal transition: only a non-terminal run may change, so
        concurrent cancellation and completion can never overwrite evidence."""
        run = self._store.get_run(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return run
        run = self._store.update_run(
            run_id, status=status, phase=None, completed_at=utc_now(), error=message
        )
        event_type = _TERMINAL_EVENT_TYPES[status]
        data: dict[str, Any] = {}
        if status == "failed":
            data = {
                "error_class": error_class or "error",
                "message": message or "run failed",
            }
            if details:
                data["details"] = details
        self._store.append_event(run_id, event_type, data)
        return run

    def _emit_report_rejected(
        self, run_id: str, exc: ReportRejectedError
    ) -> None:
        """Emit a dedicated, safe report_rejected observable event.

        The event carries only the sanitized deterministic message and safe
        validator details; the raw model response is never logged or stored.
        """
        safe_details = exc.safe_details or {}
        data: dict[str, Any] = {
            "error_class": "report_rejected",
            "message": str(exc),
            "validation_summary": safe_details,
        }
        logger.info("run=%s report rejected: %s", run_id, str(exc))
        self._store.append_event(run_id, "report_rejected", data)

    def _emit_model_failed(self, run_id: str, exc: ModelTransportError) -> None:
        """Emit a dedicated, safe model_failed event for investigator model
        transport failures (e.g. rejected model credentials).

        The event carries only the sanitized message and coarse safe details:
        never the response body, the API key, the endpoint, or raw model output.
        """
        data: dict[str, Any] = {
            "error_class": "model_failed",
            "message": str(exc),
            "details": exc.safe_details or {},
        }
        logger.info("run=%s model request failed: %s", run_id, str(exc))
        self._store.append_event(run_id, "model_failed", data)

    def _forget(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        self._cancel_events.pop(run_id, None)
        self._active_runners.pop(run_id, None)