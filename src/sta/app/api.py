"""FastAPI application (Runtime_Environments_UI.md #18, #24, #29, #30, #48).

One application owns the HTTP API, the SSE progress streams and the static
vanilla UI. :func:`create_app` is dependency-injectable: pass explicit
:class:`~sta.app.runs.RunComponents` and :class:`~sta.config.Settings` for
tests and alternative deployments; call it without arguments for the default
config-based startup (env vars per Runtime_Environments_UI.md #49).

Deployment runs the module-level ASGI object (Runtime_Environments_UI.md #16):

    uvicorn sta.app.api:app --reload

``app`` is import-safe: importing this module never reads deployment
configuration, and the default config-based app is built lazily at the first
ASGI call — the lifespan startup — where default-config fail-fast (#49)
becomes a fatal ASGI startup error instead of an import-time crash.

The API stays small (Runtime_Environments_UI.md #29):

    POST /api/runs                        create a run (exactly table_name)
    GET  /api/runs/{run_id}               run status
    GET  /api/runs/{run_id}/events        SSE progress stream (with replay)
    GET  /api/runs/{run_id}/results       stored-result index
    GET  /api/runs/{run_id}/results/{rid} one stored result (audit view)
    GET  /api/runs/{run_id}/report        final report
    POST /api/runs/{run_id}/cancel        cancel an active/queued run
    GET  /api/environment                 active environment badge

plus the static UI (plain HTML/CSS/JS) served from ``sta.ui``.
"""

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict
from starlette.types import Receive, Scope, Send

from sta.app.events import (
    DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    DEFAULT_POLL_INTERVAL_SECONDS,
    event_stream,
    parse_last_event_id,
)
from sta.app.runs import (
    RunComponents,
    RunService,
    UnconfiguredMetadataProvider,
    unconfigured_backend_factory,
)
from sta.config import Settings, get_settings
from sta.execution.backends.base import require_qualified_identifier
from sta.execution.backends.local_catalog import LocalCatalogProvider
from sta.execution.errors import BackendExecutionError
from sta.investigator.agent import create_investigator
from sta.knowledge.repository import KnowledgeBase
from sta.results.models import QueryResult, RunRecord
from sta.results.store import ResultStore

UI_DIR = Path(__file__).resolve().parent.parent / "ui"

_SAFE_404_RUN = "unknown run"
_SAFE_404_RESULT = "unknown result"
_SAFE_REPORT_NOT_READY = "report is not ready for this run"


class CreateRunRequest(BaseModel):
    """The only accepted run input: exactly one ``table_name`` value. Extra
    fields (diagnostic questions, SQL, options) are rejected with 422."""

    model_config = ConfigDict(extra="forbid")

    table_name: str


def validate_table_name(value: str) -> str:
    """The user supplies a fully qualified catalog.schema.table name and nothing else."""
    name = value.strip()
    if not name:
        raise HTTPException(status_code=422, detail="table_name must not be empty")
    try:
        validated = require_qualified_identifier(name, "table_name")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # The API contract requires catalog.namespace.table. Two-part identifiers
    # remain an internal catalog capability, never an alternate user input.
    if validated.count(".") != 2:
        raise HTTPException(
            status_code=422,
            detail=f"table_name must be a fully qualified catalog.namespace.table, got {name!r}",
        )
    return validated


def validate_settings(settings: Settings) -> None:
    """Fail fast on an unsupported environment name
    (Runtime_Environments_UI.md #49). The selected environment's required
    connection settings are checked when default components are built."""
    if settings.sta_env not in ("local", "production"):
        raise RuntimeError(
            f"unsupported STA_ENV {settings.sta_env!r} (local or production)"
        )


def default_components(settings: Settings) -> RunComponents:
    """Config-based component construction (Runtime_Environments_UI.md #49).

    Local: tables resolve through the configured PyIceberg REST catalog
    (``ICEBERG_CATALOG_URI``/``ICEBERG_WAREHOUSE`` plus S3 object-storage
    credentials) and every run executes on the PyIceberg-derived local
    backend. Missing required configuration fails fast, here, with an
    actionable message — an unconfigured local runtime is never silently
    accepted.

    Production: the IOMETE connection is validated (endpoint/catalog), but
    live production execution is not part of the local MVP wiring, so runs
    fail closed with a typed, safe error at table resolution until the
    production adapter is deployed.

    The Investigator stays fail-closed as well: without a configured model
    (``STA_INVESTIGATOR_MODEL`` or the Ollama investigator enabled by an Ollama
    API key — model exactly ``gpt-oss:120b-cloud``) each run fails with a typed
    configuration error instead of a silent fallback.
    """
    try:
        settings.validate_environment()
    except ValueError as exc:
        raise RuntimeError(str(exc)) from None
    if settings.sta_env == "local":
        try:
            provider = LocalCatalogProvider.from_settings(settings)
        except BackendExecutionError as exc:
            # Startup must fail fast with a safe, actionable RuntimeError
            # (Runtime_Environments_UI.md #49; never leak credentials).
            raise RuntimeError(exc.message) from None
        metadata_provider = provider
        backend_factory = provider.create_backend
    else:
        metadata_provider = UnconfiguredMetadataProvider(settings.sta_env)
        backend_factory = unconfigured_backend_factory(settings.sta_env)
    return RunComponents(
        store=ResultStore(settings.db_path),
        knowledge=KnowledgeBase(settings.knowledge_path),
        metadata_provider=metadata_provider,
        backend_factory=backend_factory,
        investigator_factory=create_investigator,
    )


def build_router(
    service: RunService,
    settings: Settings,
    *,
    sse_poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sse_heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/runs")
    async def create_run(request: CreateRunRequest) -> RunRecord:
        table_name = validate_table_name(request.table_name)
        return await service.start_run(table_name)

    @router.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> RunRecord:
        run = service.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=_SAFE_404_RUN)
        return run

    @router.get("/api/runs/{run_id}/results")
    async def list_results(run_id: str) -> dict[str, Any]:
        _require_run(service, run_id)
        results = service.store.list_results(run_id)
        return {"run_id": run_id, "results": [_result_summary(result) for result in results]}

    @router.get("/api/runs/{run_id}/results/{result_id}")
    async def get_result(run_id: str, result_id: str) -> QueryResult:
        _require_run(service, run_id)
        result = service.store.get_result(run_id, result_id)
        if result is None:
            raise HTTPException(status_code=404, detail=_SAFE_404_RESULT)
        return result

    @router.get("/api/runs/{run_id}/report")
    async def get_report(run_id: str) -> dict[str, Any]:
        _require_run(service, run_id)
        report = service.store.get_report(run_id)
        if report is None:
            raise HTTPException(status_code=404, detail=_SAFE_REPORT_NOT_READY)
        return report

    @router.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> RunRecord:
        run = await service.cancel_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail=_SAFE_404_RUN)
        return run

    @router.get("/api/runs/{run_id}/events")
    async def stream_events(
        run_id: str, request: Request, after_event_id: int | None = None
    ) -> Any:
        _require_run(service, run_id)
        # A reconnecting EventSource sends Last-Event-ID; it wins over the
        # explicit query parameter (Runtime_Environments_UI.md #44).
        last_id = parse_last_event_id(request.headers.get("last-event-id"), after_event_id)
        return StreamingResponse(
            event_stream(
                service.store,
                run_id,
                after_event_id=last_id,
                poll_interval=sse_poll_interval,
                heartbeat_interval=sse_heartbeat_interval,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @router.get("/api/environment")
    async def environment() -> dict[str, str]:
        return {"environment": settings.sta_env, "badge": settings.environment_badge}

    # -- static UI (plain HTML/CSS/JS, no build pipeline) -------------------

    @router.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(UI_DIR / "index.html", media_type="text/html")

    @router.get("/app.js", include_in_schema=False)
    async def app_js() -> FileResponse:
        return FileResponse(UI_DIR / "app.js", media_type="text/javascript")

    @router.get("/styles.css", include_in_schema=False)
    async def styles_css() -> FileResponse:
        return FileResponse(UI_DIR / "styles.css", media_type="text/css")

    return router


def _require_run(service: RunService, run_id: str) -> RunRecord:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=_SAFE_404_RUN)
    return run


def _result_summary(result: QueryResult) -> dict[str, Any]:
    """Light index entry for the result list (Runtime_Environments_UI.md #28);
    the payload stays in the per-result detail view."""
    return {
        "result_id": result.result_id,
        "tool_name": result.tool_name,
        "query_version": result.query_version,
        "snapshot_id": result.snapshot_id,
        "row_count": result.row_count,
        "duration_ms": result.duration_ms,
        "executed_at": result.executed_at,
    }


def create_app(
    components: RunComponents | None = None,
    settings: Settings | None = None,
    *,
    sse_poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sse_heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
) -> FastAPI:
    """Build the FastAPI application. Without arguments this is the default
    config-based startup; tests inject components/settings explicitly."""
    settings = settings or get_settings()
    validate_settings(settings)
    owns_store = components is None
    components = components or default_components(settings)
    service = RunService(
        components,
        max_concurrent_runs=settings.max_concurrent_runs,
        query_timeout_seconds=settings.query_timeout_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await service.shutdown()
            if owns_store:
                components.store.close()

    app = FastAPI(title="Smart Table Analyzer", lifespan=lifespan)
    app.include_router(
        build_router(
            service,
            settings,
            sse_poll_interval=sse_poll_interval,
            sse_heartbeat_interval=sse_heartbeat_interval,
        )
    )
    return app


class _LazyApp:
    """Import-safe ASGI object behind the documented entrypoint
    ``uvicorn sta.app.api:app --reload`` (Runtime_Environments_UI.md #16).

    Importing this module must not require deployment configuration (tests
    import :func:`create_app` without any), so the default app is built on
    the first ASGI call instead of at import time. The lifespan startup is
    the build point: a configuration failure is reported as
    ``lifespan.startup.failed`` there, which uvicorn treats as a fatal
    startup error — default-config fail-fast (#49) happens at application
    startup, never at Python import.
    """

    def __init__(self, factory: Callable[[], FastAPI]) -> None:
        self._factory = factory
        self._app: FastAPI | None = None
        self._lock = threading.Lock()

    def build(self) -> FastAPI:
        """Build the default app once; raises on configuration errors."""
        if self._app is None:
            with self._lock:
                if self._app is None:
                    self._app = self._factory()
        return self._app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            try:
                app = self.build()
            except Exception as exc:
                # uvicorn's default lifespan="auto" treats an exception inside
                # the lifespan scope as "protocol unsupported" and would keep
                # serving; the explicit startup.failed event is the fail-fast
                # signal that stops the server with the actionable message.
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
                return
            await app(scope, receive, send)
            return
        await self.build()(scope, receive, send)


#: Default ASGI entrypoint for deployment: ``uvicorn sta.app.api:app``.
app = _LazyApp(create_app)