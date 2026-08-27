"""FastAPI composition root for the process-wide Yoke HTTP API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
import secrets
from uuid import uuid4

from fastapi import FastAPI
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from yoke.http.errors import ApiError
from yoke.http.errors import api_error_handler
from yoke.http.errors import error_response
from yoke.http.errors import validation_error_handler
from yoke.http.routes import health
from yoke.http.routes import catalog
from yoke.http.routes import command
from yoke.http.routes import event
from yoke.http.routes import filesystem
from yoke.http.routes import location
from yoke.http.routes import mcp
from yoke.http.routes import permission
from yoke.http.routes import prompt
from yoke.http.routes import process
from yoke.http.routes import question
from yoke.http.routes import session
from yoke.http.routes import skill
from yoke.http.routes import tool_trace
from yoke.http.routes import tool
from yoke.http.routes import upload
from yoke.http.services.location_service import LocationService
from yoke.http.services.mcp_service import McpService
from yoke.http.services.catalog_service import CatalogService
from yoke.http.services.event_broker import EventService
from yoke.http.services.event_broker import GlobalEventBroker
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.filesystem_service import FilesystemService
from yoke.http.services.human_input_service import HumanInputService
from yoke.http.services.path_policy import PathPolicy
from yoke.http.services.process_service import ProcessService
from yoke.http.services.runtime_factory import build_http_session_agent
from yoke.http.services.runtime_factory import SessionAgentFactory
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.http.services.session_service import SessionService
from yoke.http.services.skill_service import SkillService
from yoke.http.services.tool_trace_service import ToolTraceService
from yoke.http.services.tool_service import ToolService
from yoke.http.services.upload_service import UploadService
from yoke.http.web import install_web_routes
from yoke.session import SessionStore
from yoke.session.admissions import AdmissionStore
from yoke.session.events import SessionEventJournal


@dataclass(frozen=True, slots=True)
class HttpAppSettings:
    """Injectable daemon settings used by production and contract tests."""

    auth_token: str | None
    session_directory: Path | None = None
    max_active_sessions: int = 4
    max_worker_threads: int | None = None
    agent_factory: SessionAgentFactory | None = None


def create_app(settings: HttpAppSettings | None = None) -> FastAPI:
    """Create one process-wide Yoke API application."""
    configured = settings or HttpAppSettings(auth_token=None)
    store = SessionStore(configured.session_directory)
    store.maintain_index(force=True)
    broker = GlobalEventBroker()
    journal = SessionEventJournal(store.directory)
    events = EventService(journal, broker)
    admissions = AdmissionStore(store.directory)
    uploads = UploadService(store)
    pending_inputs = PendingInputService(store, admissions, events, uploads)
    runtime_registry = SessionRuntimeRegistry(
        store=store,
        pending_inputs=pending_inputs,
        events=events,
        agent_factory=configured.agent_factory or build_http_session_agent,
        max_active_sessions=configured.max_active_sessions,
        max_worker_threads=configured.max_worker_threads,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        maintenance_task = asyncio.create_task(_maintain_session_index(store))
        try:
            yield
        finally:
            maintenance_task.cancel()
            with suppress(asyncio.CancelledError):
                await maintenance_task
            await runtime_registry.close()

    app = FastAPI(
        title="Yoke HTTP API",
        version="1",
        openapi_url="/api/v1/openapi.json",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.auth_token = configured.auth_token
    app.state.server_instance_id = f"srv_{secrets.token_hex(8)}"
    app.state.event_broker = broker
    app.state.event_journal = journal
    app.state.event_service = events
    app.state.session_service = SessionService(store, events)
    app.state.skill_service = SkillService(store)
    app.state.location_service = LocationService(store)
    app.state.human_input_service = HumanInputService(store, events)
    app.state.mcp_service = McpService(store, runtime_registry, events)
    app.state.catalog_service = CatalogService()
    app.state.pending_input_service = pending_inputs
    app.state.path_policy = PathPolicy()
    app.state.filesystem_service = FilesystemService(app.state.path_policy)
    app.state.runtime_registry = runtime_registry
    app.state.process_service = ProcessService(runtime_registry)
    app.state.tool_trace_service = ToolTraceService(store, runtime_registry)
    app.state.tool_service = ToolService(store, runtime_registry)
    app.state.upload_service = uploads

    @app.middleware("http")
    async def request_identity(request: Request, call_next):  # noqa: ANN001,ANN202
        request.state.request_id = request.headers.get("x-request-id") or f"req_{uuid4().hex}"
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):  # noqa: ANN202
        return await api_error_handler(request, exc)

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
        )

    app.include_router(health.router, prefix="/api/v1")
    app.include_router(catalog.router, prefix="/api/v1")
    app.include_router(command.router, prefix="/api/v1")
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
    install_web_routes(app)
    return app


async def _maintain_session_index(store: SessionStore) -> None:
    """Keep filesystem repair and retention work off HTTP request threads."""
    while True:
        await asyncio.sleep(5)
        await asyncio.to_thread(store.maintain_index)
