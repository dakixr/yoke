"""FastAPI composition root for the process-wide Yoke HTTP API."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from yoke.http.errors import (
    ApiError,
    api_error_handler,
    error_response,
    validation_error_handler,
    workspace_error_handler,
)
from yoke.http.routes import (
    agent_run,
    catalog,
    client_session,
    command,
    daemon,
    event,
    filesystem,
    health,
    location,
    mcp,
    permission,
    process,
    prompt,
    question,
    session,
    skill,
    tool,
    tool_trace,
    upload,
    workspace,
)
from yoke.http.services.agent_runs import AgentRunService
from yoke.http.services.catalog_service import CatalogService
from yoke.http.services.event_broker import EventService, GlobalEventBroker
from yoke.http.services.filesystem_service import FilesystemService
from yoke.http.services.human_input_service import HumanInputService
from yoke.http.services.location_service import LocationService
from yoke.http.services.mcp_service import McpService
from yoke.http.services.path_policy import PathPolicy
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.process_service import ProcessService
from yoke.http.runtime.registry import (
    SessionAgentFactory,
    SessionRuntimeRegistry,
)
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.reads.cache import SessionReadCache
from yoke.http.services.session_service import SessionService
from yoke.http.services.skill_service import SkillService
from yoke.http.services.thread_settlement import ThreadSettlementService
from yoke.http.services.tool_service import ToolService
from yoke.http.services.tool_trace_service import ToolTraceService
from yoke.http.services.upload_service import UploadService
from yoke.http.web import install_web_routes
from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionStore
from yoke.session.events import SessionEventJournal
from yoke.session.workspace import WorkspaceError


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HttpAppSettings:
    """Injectable daemon settings used by production and contract tests."""

    auth_token: str | None
    session_directory: Path | None = None
    max_active_sessions: int = 50
    max_worker_threads: int | None = None
    agent_factory: SessionAgentFactory | None = None
    indexed_runtime_seed: bool = True
    # Set by ``yoke serve --daemon``: leases, idle exit and the peer-checked socket.
    daemon: DaemonLifecycle | None = None
    daemon_socket: str | None = None


def create_app(settings: HttpAppSettings | None = None) -> FastAPI:
    """Create one process-wide Yoke API application."""
    configured = settings or HttpAppSettings(auth_token=None)
    # The daemon composition root owns schema creation and upgrades. Library
    # readers and administration commands require an exact schema by default.
    store = SessionStore(configured.session_directory, migrate=True)
    broker = GlobalEventBroker()
    journal = SessionEventJournal(store.directory)
    events = EventService(journal, broker)
    admissions = AdmissionStore(store.directory)
    uploads = UploadService(store)
    pending_inputs = PendingInputService(store, admissions, events, uploads)
    read_cache = SessionReadCache(store)
    message_index = SessionMessageIndex(store)
    session_service = SessionService(
        store, events, read_cache=read_cache, message_index=message_index
    )
    runtime_registry = SessionRuntimeRegistry(
        store=store,
        pending_inputs=pending_inputs,
        events=events,
        agent_factory=configured.agent_factory or _build_http_session_agent,
        read_cache=read_cache,
        message_index=message_index,
        indexed_runtime_seed=configured.indexed_runtime_seed,
        max_active_sessions=configured.max_active_sessions,
        max_worker_threads=configured.max_worker_threads,
    )
    agent_runs = AgentRunService(runtime_registry, events)
    thread_settlement = ThreadSettlementService(session_service, runtime_registry)
    lifecycle = configured.daemon
    if lifecycle is not None:
        from yoke.daemon.work import DaemonWork

        lifecycle.bind_busy_probe(DaemonWork(store, runtime_registry).busy)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        agent_run_task = asyncio.create_task(agent_runs.watch())
        settlement_task = asyncio.create_task(thread_settlement.watch())
        maintenance_task = asyncio.create_task(
            _maintain_session_index(
                store,
                initial_delay_seconds=2 if store.has_session_index() else 0,
            )
        )
        runtime_import_task = asyncio.create_task(
            _warm_runtime_import(initial_delay_seconds=1.5)
        )
        background = [
            settlement_task,
            maintenance_task,
            runtime_import_task,
            agent_run_task,
        ]
        if lifecycle is not None:
            background.append(asyncio.create_task(lifecycle.watch()))
        try:
            yield
        finally:
            for task in background:
                task.cancel()
            try:
                results = await asyncio.gather(*background, return_exceptions=True)
                for result in results:
                    if isinstance(result, Exception):
                        LOGGER.error(
                            "Daemon background task failed during shutdown",
                            exc_info=result,
                        )
            finally:
                broker.close()
                try:
                    await runtime_registry.close_runtimes()
                finally:
                    agent_runs.close()
                    try:
                        session_service.close()
                    finally:
                        runtime_registry.shutdown_executor()

    app = FastAPI(
        title="Yoke HTTP API",
        version="1",
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.auth_token = configured.auth_token
    app.state.daemon = lifecycle
    app.state.daemon_socket = configured.daemon_socket
    app.state.server_instance_id = f"srv_{secrets.token_hex(8)}"
    app.state.event_broker = broker
    app.state.event_journal = journal
    app.state.event_service = events
    app.state.session_service = session_service
    app.state.thread_settlement_service = thread_settlement
    app.state.skill_service = SkillService(store)
    app.state.location_service = LocationService(store)
    app.state.human_input_service = HumanInputService(store, events)
    app.state.mcp_service = McpService(store, runtime_registry, events)
    app.state.catalog_service = CatalogService()
    app.state.pending_input_service = pending_inputs
    app.state.path_policy = PathPolicy()
    app.state.filesystem_service = FilesystemService(app.state.path_policy)
    app.state.runtime_registry = runtime_registry
    app.state.agent_run_service = agent_runs
    app.state.process_service = ProcessService(runtime_registry)
    app.state.tool_trace_service = ToolTraceService(
        store,
        runtime_registry,
        privacy=events.privacy,
        read_cache=read_cache,
        message_index=message_index,
    )
    app.state.tool_service = ToolService(store, runtime_registry)
    app.state.upload_service = uploads

    @app.middleware("http")
    async def request_identity(request: Request, call_next):  # noqa: ANN001,ANN202
        request.state.request_id = (
            request.headers.get("x-request-id") or f"req_{uuid4().hex}"
        )
        mutation = (
            lifecycle is not None
            and request.method in {"POST", "PUT", "PATCH", "DELETE"}
            and request.url.path.startswith("/api/v1/session")
            and not request.url.path.endswith(("/wait", "/drain"))
        )
        if lifecycle is not None and lifecycle.exit_reason is not None:
            return error_response(
                request,
                status_code=503,
                code="daemon_exiting",
                message="The daemon is shutting down; reconnect.",
            )
        if mutation and lifecycle is not None:
            lifecycle.begin_mutation()
        try:
            response = await call_next(request)
        finally:
            if mutation and lifecycle is not None:
                lifecycle.end_mutation()
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):  # noqa: ANN202
        return await api_error_handler(request, exc)

    @app.exception_handler(WorkspaceError)
    async def handle_workspace_error(request: Request, exc: WorkspaceError):  # noqa: ANN202
        return await workspace_error_handler(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(  # noqa: ANN202
        request: Request,
        exc: RequestValidationError,
    ):
        return await validation_error_handler(request, exc)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(  # noqa: ANN202
        request: Request,
        exc: StarletteHTTPException,
    ):
        code = "unauthorized" if exc.status_code == 401 else "http_error"
        return error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail),
            headers=exc.headers,
        )

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(client_session.router, prefix="/api/v1")
    app.include_router(agent_run.router, prefix="/api/v1")
    app.include_router(catalog.router, prefix="/api/v1")
    app.include_router(command.router, prefix="/api/v1")
    app.include_router(daemon.router, prefix="/api/v1")
    app.include_router(event.router, prefix="/api/v1")
    app.include_router(filesystem.router, prefix="/api/v1")
    app.include_router(location.router, prefix="/api/v1")
    app.include_router(mcp.router, prefix="/api/v1")
    app.include_router(permission.router, prefix="/api/v1")
    app.include_router(prompt.router, prefix="/api/v1")
    app.include_router(process.router, prefix="/api/v1")
    app.include_router(question.router, prefix="/api/v1")
    app.include_router(session.router, prefix="/api/v1")
    app.include_router(skill.router, prefix="/api/v1")
    app.include_router(tool_trace.router, prefix="/api/v1")
    app.include_router(tool.router, prefix="/api/v1")
    app.include_router(upload.router, prefix="/api/v1")
    app.include_router(workspace.router, prefix="/api/v1")
    install_web_routes(app)
    return app


def _build_http_session_agent(record):  # noqa: ANN001,ANN202
    """Import the CLI/provider runtime only when a session actually needs an agent."""
    from yoke.http.runtime.factory import build_http_session_agent

    return build_http_session_agent(record)


async def _maintain_session_index(
    store: SessionStore,
    *,
    initial_delay_seconds: float = 0,
) -> None:
    """Keep filesystem repair and retention work off HTTP request threads."""
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    force = True
    backoff = 5.0
    while True:
        try:
            await asyncio.to_thread(store.maintain_index, force=force)
        except Exception:  # noqa: BLE001 - maintenance must survive corrupt data and transient I/O
            LOGGER.exception("Session index maintenance failed; retrying")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        else:
            force = False
            backoff = 5.0
            await asyncio.sleep(5)


async def _warm_runtime_import(*, initial_delay_seconds: float) -> None:
    """Warm HTTP turn imports after first paint without blocking server startup."""
    if initial_delay_seconds:
        await asyncio.sleep(initial_delay_seconds)
    await asyncio.to_thread(_load_runtime_module)


def _load_runtime_module() -> None:
    import yoke.http.runtime.runtime  # noqa: F401,PLC0415
