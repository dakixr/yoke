"""Permission request HTTP resources."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.human_input import PermissionListResponse
from yoke.http.models.human_input import PermissionReplyRequest
from yoke.http.models.human_input import PermissionReplyResponse
from yoke.http.services.human_input_service import HumanInputService


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> HumanInputService:
    return request.app.state.human_input_service


@router.get(
    "/session/{session_id}/permission",
    response_model=PermissionListResponse,
    operation_id="listSessionPermissions",
)
def list_session_permissions(
    request: Request,
    session_id: str,
) -> PermissionListResponse:
    return PermissionListResponse(data=_service(request).permissions(session_id))


@router.post(
    "/session/{session_id}/permission/{request_id}/reply",
    response_model=PermissionReplyResponse,
    operation_id="replySessionPermission",
)
def reply_session_permission(
    request: Request,
    session_id: str,
    request_id: str,
    body: PermissionReplyRequest,
) -> PermissionReplyResponse:
    resolution = _service(request).reply_permission(
        session_id,
        request_id,
        reply=body.reply,
        message=body.message,
    )
    return PermissionReplyResponse(data=resolution)
