"""Session repository, message, and tree routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.session import MessageListResponse
from yoke.http.models.session import MessageResponse
from yoke.http.models.session import ContextResponse
from yoke.http.models.session import SessionActiveResponse
from yoke.http.models.session import SessionCreateRequest
from yoke.http.models.session import SessionCompactionData
from yoke.http.models.session import SessionCompactionRequest
from yoke.http.models.session import SessionCompactionResponse
from yoke.http.models.session import SessionForkRequest
from yoke.http.models.session import SessionListResponse
from yoke.http.models.session import SessionPatchRequest
from yoke.http.models.session import SessionResponse
from yoke.http.models.session import SessionSelection
from yoke.http.models.session import SessionSelectionRequest
from yoke.http.models.session import SessionSelectionResponse
from yoke.http.models.session import SessionSelectionResult
from yoke.http.models.session import TreeResponse
from yoke.http.models.session import TreeEntryPatchRequest
from yoke.http.models.session import TreeEntryPatchResponse
from yoke.http.models.session import TreeNavigateRequest
from yoke.http.models.session import TreeNavigateResponse
from yoke.http.models.session import TreeNavigationPreviewResponse
from yoke.http.services.session_service import SessionService
from yoke.http.services.runtime_registry import SessionRuntimeRegistry


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> SessionService:
    return request.app.state.session_service


def _runtimes(request: Request) -> SessionRuntimeRegistry:
    return request.app.state.runtime_registry


@router.get("/session", response_model=SessionListResponse, operation_id="listSessions")
def list_sessions(
    request: Request,
    directory: str | None = Query(default=None),
    search: str | None = Query(default=None),
    pinned: bool | None = Query(default=None),
    archived: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    order: Literal["updatedDesc", "updatedAsc", "createdDesc", "createdAsc"] = Query(
        default="updatedDesc"
    ),
    cursor: str | None = Query(default=None),
) -> SessionListResponse:
    return _service(request).list_sessions(
        directory=directory,
        search=search,
        pinned=pinned,
        archived=archived,
        limit=limit,
        order=order,
        cursor=cursor,
    )


@router.post("/session", response_model=SessionResponse, operation_id="createSession")
def create_session(request: Request, body: SessionCreateRequest) -> SessionResponse:
    return SessionResponse(data=_service(request).create_session(body))


@router.get(
    "/session/active",
    response_model=SessionActiveResponse,
    operation_id="activeSessions",
)
async def active_sessions(request: Request) -> SessionActiveResponse:
    registry: SessionRuntimeRegistry = request.app.state.runtime_registry
    return SessionActiveResponse(data=await registry.active_snapshot())


@router.get(
    "/session/{session_id}",
    response_model=SessionResponse,
    operation_id="getSession",
)
def get_session(request: Request, session_id: str) -> SessionResponse:
    return SessionResponse(data=_service(request).get_session(session_id))


@router.patch(
    "/session/{session_id}",
    response_model=SessionResponse,
    operation_id="patchSession",
)
async def patch_session(
    request: Request,
    session_id: str,
    body: SessionPatchRequest,
) -> SessionResponse:
    fields = body.model_fields_set
    if "title" in fields:
        _runtimes(request).cancel_automatic_title(session_id)
    return SessionResponse(
        data=_service(request).patch_session(
            session_id,
            title_set="title" in fields,
            title=body.title,
            pinned_set="pinned" in fields,
            pinned=body.pinned,
            archived_set="archived" in fields,
            archived=body.archived,
        )
    )


@router.post(
    "/session/{session_id}/fork",
    response_model=SessionResponse,
    operation_id="forkSession",
)
def fork_session(
    request: Request,
    session_id: str,
    body: SessionForkRequest,
) -> SessionResponse:
    return SessionResponse(data=_service(request).fork_session(session_id, body))


@router.post(
    "/session/{session_id}/selection",
    response_model=SessionSelectionResponse,
    operation_id="selectSessionModel",
)
async def select_session_model(
    request: Request,
    session_id: str,
    body: SessionSelectionRequest,
) -> SessionSelectionResponse:
    _service(request).get_session(session_id)
    state = await _runtimes(request).select_model(
        session_id,
        provider_name=body.provider,
        model_id=body.model,
        reasoning_effort=body.reasoning_effort,
    )
    return SessionSelectionResponse(
        data=SessionSelectionResult(
            effective=SessionSelection(
                provider=state.provider_name,
                model=state.model_id,
                reasoning_effort=state.reasoning_effort,
            )
        )
    )


@router.post(
    "/session/{session_id}/compact",
    response_model=SessionCompactionResponse,
    status_code=202,
    operation_id="compactSession",
)
async def compact_session(
    request: Request,
    session_id: str,
    body: SessionCompactionRequest,
) -> SessionCompactionResponse:
    del body
    _service(request).get_session(session_id)
    operation_id = await _runtimes(request).compact(session_id)
    return SessionCompactionResponse(
        data=SessionCompactionData(operation_id=operation_id)
    )


@router.post(
    "/session/{session_id}/title/regenerate",
    response_model=SessionResponse,
    operation_id="regenerateSessionTitle",
)
async def regenerate_session_title(
    request: Request,
    session_id: str,
) -> SessionResponse:
    service = _service(request)
    service.get_session(session_id)
    runtimes = _runtimes(request)
    runtimes.cancel_automatic_title(session_id)
    title = await runtimes.regenerate_title(session_id)
    return SessionResponse(
        data=service.patch_session(
            session_id,
            title_set=True,
            title=title,
            pinned_set=False,
            pinned=None,
            archived_set=False,
            archived=None,
        )
    )


@router.get(
    "/session/{session_id}/message",
    response_model=MessageListResponse,
    operation_id="listSessionMessages",
)
def list_messages(
    request: Request,
    session_id: str,
    limit: int = Query(default=50, ge=1, le=200),
    order: Literal["asc", "desc"] = Query(default="desc"),
    cursor: str | None = Query(default=None),
    branch: Literal["active"] = Query(default="active"),
) -> MessageListResponse:
    del branch
    return _service(request).messages(
        session_id,
        limit=limit,
        order=order,
        cursor=cursor,
    )


@router.get(
    "/session/{session_id}/message/{message_id}",
    response_model=MessageResponse,
    operation_id="getSessionMessage",
)
def get_message(
    request: Request,
    session_id: str,
    message_id: str,
) -> MessageResponse:
    return _service(request).message(session_id, message_id)


@router.get(
    "/session/{session_id}/context",
    response_model=ContextResponse,
    operation_id="getSessionContext",
)
def get_context(
    request: Request,
    session_id: str,
    include_system: bool = Query(default=False, alias="includeSystem"),
    include_tool_results: bool = Query(default=True, alias="includeToolResults"),
    limit: int = Query(default=100, ge=1, le=1000),
    max_chars: int = Query(default=500_000, ge=1_000, le=2_000_000, alias="maxChars"),
) -> ContextResponse:
    return _service(request).context(
        session_id,
        include_system=include_system,
        include_tool_results=include_tool_results,
        limit=limit,
        max_chars=max_chars,
    )


@router.get(
    "/session/{session_id}/tree",
    response_model=TreeResponse,
    operation_id="getSessionTree",
)
def get_tree(
    request: Request,
    session_id: str,
    limit: int = Query(default=200, ge=1, le=2000),
    cursor: str | None = Query(default=None),
) -> TreeResponse:
    return _service(request).tree(session_id, limit=limit, cursor=cursor)


@router.get(
    "/session/{session_id}/tree/navigation-preview",
    response_model=TreeNavigationPreviewResponse,
    operation_id="previewSessionTreeNavigation",
)
def preview_tree_navigation(
    request: Request,
    session_id: str,
    target_id: str = Query(alias="targetID"),
    include_abandoned: bool = Query(default=True, alias="includeAbandoned"),
) -> TreeNavigationPreviewResponse:
    return _service(request).navigation_preview(
        session_id,
        target_id=target_id,
        include_abandoned=include_abandoned,
    )


@router.post(
    "/session/{session_id}/tree/navigate",
    response_model=TreeNavigateResponse,
    operation_id="navigateSessionTree",
)
async def navigate_tree(
    request: Request,
    session_id: str,
    body: TreeNavigateRequest,
) -> TreeNavigateResponse:
    registry: SessionRuntimeRegistry = request.app.state.runtime_registry
    service = _service(request)
    return await registry.idle_mutation(
        session_id,
        lambda: service.navigate_tree(session_id, body),
    )


@router.patch(
    "/session/{session_id}/tree/{entry_id}",
    response_model=TreeEntryPatchResponse,
    operation_id="patchSessionTreeEntry",
)
async def patch_tree_entry(
    request: Request,
    session_id: str,
    entry_id: str,
    body: TreeEntryPatchRequest,
) -> TreeEntryPatchResponse:
    registry: SessionRuntimeRegistry = request.app.state.runtime_registry
    service = _service(request)
    return await registry.idle_mutation(
        session_id,
        lambda: service.set_tree_label(
            session_id,
            entry_id,
            expected_revision=body.expected_revision,
            label=body.label,
        ),
    )
