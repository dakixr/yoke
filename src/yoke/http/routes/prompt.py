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
    SessionInputsResponse,
    InputResponse,
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
    runtime = _registry(request).get_or_start(session_id)
    receipt = await asyncio.get_running_loop().run_in_executor(
        _registry(request).storage_executor,
        runtime.resources.configuration.clients.admit,
        _pending(request),
        session_id,
        body,
    )
    if body.resume:
        await runtime.wake()
    else:
        await runtime.hold_if_idle()
    receipt = await asyncio.get_running_loop().run_in_executor(
        _registry(request).storage_executor,
        _pending(request).receipt,
        session_id,
        receipt.id,
    )
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
    await asyncio.get_running_loop().run_in_executor(
        _registry(request).storage_executor,
        _registry(request)
        .get_or_start(session_id)
        .resources.configuration.clients.patch_queue,
        _pending(request),
        session_id,
        body,
    )
    runtime = _registry(request).get_or_start(session_id)
    if body.resume:
        await runtime.wake()
    else:
        await runtime.hold_if_idle()
    return QueueResponse(
        data=await asyncio.to_thread(_pending(request).queue, session_id)
    )


@router.post(
    "/session/{session_id}/interrupt",
    response_model=InterruptResponse,
    operation_id="interruptSession",
)
async def interrupt_session(request: Request, session_id: str) -> InterruptResponse:
    _sessions(request).get_session(session_id)
    runtime = _registry(request).get_if_loaded(session_id)
    result = (
        await runtime.interrupt_result()
        if runtime is not None
        else InterruptData(interrupted=False)
    )
    return InterruptResponse(data=result)


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


@router.get(
    "/session/{session_id}/input",
    response_model=SessionInputsResponse,
    operation_id="getSessionInputs",
)
def get_inputs(
    request: Request, session_id: str, live: bool = True
) -> SessionInputsResponse:
    return SessionInputsResponse(
        data=_pending(request).control.snapshot(session_id, live=live)
    )


@router.get(
    "/session/{session_id}/input/{input_id}",
    response_model=InputResponse,
    operation_id="getSessionInput",
)
def get_input(request: Request, session_id: str, input_id: str) -> InputResponse:
    control = _pending(request).control
    return InputResponse(data=control.info(control.get_input(session_id, input_id)))


@router.post(
    "/session/{session_id}/queue/resume",
    response_model=QueueResponse,
    operation_id="resumeSessionQueue",
)
async def resume_queue(request: Request, session_id: str) -> QueueResponse:
    await _registry(request).get_or_start(session_id).wake()
    return QueueResponse(
        data=await asyncio.to_thread(_pending(request).queue, session_id)
    )


@router.post(
    "/session/{session_id}/input/{input_id}/cancel",
    response_model=InputResponse,
    operation_id="cancelSessionInput",
)
async def cancel_input(
    request: Request, session_id: str, input_id: str
) -> InputResponse:
    runtime = _registry(request).get_or_start(session_id)
    await runtime.cancel_input(input_id)
    return get_input(request, session_id, input_id)
