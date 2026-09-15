"""Explicit recovery of an existing session's execution directory."""

from fastapi import APIRouter, Depends, Request

from yoke.http.auth import require_auth
from yoke.http.models.session import SessionRelocateRequest, SessionResponse
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.http.services.session_service import SessionService

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post(
    "/session/{session_id}/relocate",
    response_model=SessionResponse,
    operation_id="relocateSession",
)
async def relocate_session(
    request: Request, session_id: str, body: SessionRelocateRequest
) -> SessionResponse:
    sessions: SessionService = request.app.state.session_service
    sessions.get_session(session_id)
    registry: SessionRuntimeRegistry = request.app.state.runtime_registry
    await registry.relocate(
        session_id, body.directory, expected_root=body.expected_directory
    )
    return SessionResponse(data=sessions.get_session(session_id))
