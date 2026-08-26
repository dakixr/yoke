"""Prompt admission, queue editing, and execution-control routes."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.prompt import PromptAdmissionRequest
from yoke.http.models.prompt import PromptAdmissionResponse
from yoke.http.models.prompt import QueuePatchRequest
from yoke.http.models.prompt import QueueResponse
from yoke.http.models.session import InterruptData
from yoke.http.models.session import InterruptResponse
from yoke.http.models.session import WaitResponse
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.services.runtime_registry import SessionRuntimeRegistry
from yoke.http.services.session_service import SessionService


router = APIRouter(dependencies=[Depends(require_auth)])


def _pending(request: Request) -> PendingInputService:
    return request.app.state.pending_input_service


def _registry(request: Request) -> SessionRuntimeRegistry:
    return request.app.state.runtime_registry


def _sessions(request: Request) -> SessionService:
    return request.app.state.session_service


@router.post(
    "/session/{session_id}/prompt",
    response_model=PromptAdmissionResponse,
    operation_id="admitSessionPrompt",
)
async def admit_prompt(
    request: Request,
    session_id: str,
    body: PromptAdmissionRequest,
) -> PromptAdmissionResponse:
    receipt = _pending(request).admit(session_id, body)
    if body.resume:
        await _registry(request).wake(session_id)
    return PromptAdmissionResponse(data=receipt)


@router.get(
    "/session/{session_id}/queue",
    response_model=QueueResponse,
    operation_id="getSessionQueue",
)
def get_queue(request: Request, session_id: str) -> QueueResponse:
    return QueueResponse(data=_pending(request).queue(session_id))


@router.patch(
    "/session/{session_id}/queue",
    response_model=QueueResponse,
    operation_id="patchSessionQueue",
)
async def patch_queue(
    request: Request,
    session_id: str,
    body: QueuePatchRequest,
) -> QueueResponse:
    data = _pending(request).patch_queue(session_id, body)
    await _registry(request).wake(session_id)
    return QueueResponse(data=data)


@router.post(
    "/session/{session_id}/interrupt",
    response_model=InterruptResponse,
    operation_id="interruptSession",
)
async def interrupt_session(request: Request, session_id: str) -> InterruptResponse:
    _sessions(request).get_session(session_id)
    interrupted, turn_id = await _registry(request).interrupt(session_id)
    return InterruptResponse(
        data=InterruptData(interrupted=interrupted, turn_id=turn_id)
    )


@router.post(
    "/session/{session_id}/wait",
    response_model=WaitResponse,
    operation_id="waitForSession",
)
async def wait_for_session(
    request: Request,
    session_id: str,
    timeout_ms: int | None = Query(
        default=None,
        alias="timeoutMs",
        ge=1,
        le=300_000,
    ),
) -> WaitResponse:
    _sessions(request).get_session(session_id)
    registry = _registry(request)
    try:
        status = await registry.wait(
            session_id,
            None if timeout_ms is None else timeout_ms / 1000,
        )
    except TimeoutError:
        runtime = registry.get_if_loaded(session_id)
        status = (
            await runtime.status() if runtime is not None else await registry.wait(session_id, 0)
        )
    return WaitResponse(data=status)
