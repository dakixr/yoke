"""Prompt admission, queue editing, and execution-control routes."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Request

from yoke.http.auth import require_auth
from yoke.http.models.drain import DrainData, DrainResponse
from yoke.http.models.prompt import (
    PromptAdmissionRequest,
    PromptAdmissionResponse,
    QueuePatchRequest,
    QueueResponse,
)
from yoke.http.models.session import InterruptData, InterruptResponse, WaitResponse
from yoke.http.services.pending_input_service import PendingInputService
from yoke.http.runtime.registry import SessionRuntimeRegistry
from yoke.http.services.session_service import SessionService

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post(
    "/session/{session_id}/drain",
    response_model=DrainResponse,
    operation_id="drainSessionWorkers",
)
async def drain_session_workers(
    request: Request,
    session_id: str,
    timeout_ms: int = Query(default=30_000, alias="timeoutMs", ge=1, le=300_000),
) -> DrainResponse:
    """Observe completion of admitted workers and their cleanup, not ownership."""
    _sessions(request).get_session(session_id)
    runtime = _registry(request).get_if_loaded(session_id)
    count = await runtime.workers.drain(timeout_ms / 1000) if runtime else 0
    return DrainResponse(data=DrainData(drained=count == 0, active_workers=count))


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
    receipt = await asyncio.get_running_loop().run_in_executor(
        _registry(request).storage_executor, _pending(request).admit, session_id, body
    )
    if body.resume:
        await _registry(request).get_or_start(session_id).wake()
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
    data = await asyncio.get_running_loop().run_in_executor(
        _registry(request).storage_executor,
        _pending(request).patch_queue,
        session_id,
        body,
    )
    await _registry(request).get_or_start(session_id).wake()
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
            await runtime.status()
            if runtime is not None
            else await registry.wait(session_id, 0)
        )
    return WaitResponse(data=status)
