"""Session-local ACP and other client configuration, never written to disk."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from yoke.http.auth import require_auth
from yoke.http.models.client_session import ClientSessionRequest
from yoke.http.services.session_runtime.configuration import (
    configure_client,
    detach_client,
)

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/session/{session_id}/client", operation_id="getSessionClient")
async def status(request: Request, session_id: str) -> dict[str, object]:
    request.app.state.session_service.get_session(session_id)
    runtime = request.app.state.runtime_registry.get_if_loaded(session_id)
    client = runtime.resources.client_session if runtime is not None else None
    return {
        "data": {"mode": client.config.mode, "plan": client.latest_plan}
        if client
        else None
    }


@router.put("/session/{session_id}/client", operation_id="configureSessionClient")
async def configure(
    request: Request, session_id: str, body: ClientSessionRequest
) -> dict[str, object]:
    runtime = request.app.state.runtime_registry.get_or_start(session_id)
    return {
        "data": await configure_client(
            runtime, body, request.app.state.human_input_service
        )
    }


@router.delete("/session/{session_id}/client", operation_id="detachSessionClient")
async def detach(request: Request, session_id: str, owner: str) -> dict[str, object]:
    runtime = request.app.state.runtime_registry.get_if_loaded(session_id)
    if runtime is not None:
        await detach_client(runtime, owner)
    return {"data": {"detached": True}}
