"""Per-user daemon status, leases, restart and stop."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from yoke.daemon.build import build_id, protocol_version
from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.http.auth import require_auth
from yoke.http.errors import ApiError
from yoke.http.models.daemon import (
    DaemonControlData,
    DaemonControlResponse,
    DaemonRestartRequest,
    DaemonStatusData,
    DaemonStatusResponse,
    DaemonStopRequest,
    LeaseInfo,
)

router = APIRouter(dependencies=[Depends(require_auth)])

LEASE_HEARTBEAT_SECONDS = 15.0


def lifecycle_of(request: Request) -> DaemonLifecycle:
    lifecycle = getattr(request.app.state, "daemon", None)
    if lifecycle is None:
        raise ApiError(404, "daemon_mode_required", "Run `yoke serve --daemon`.")
    return lifecycle


@router.get("/daemon", response_model=DaemonStatusResponse, operation_id="daemonStatus")
async def daemon_status(request: Request) -> DaemonStatusResponse:
    lifecycle = lifecycle_of(request)
    return DaemonStatusResponse(
        data=DaemonStatusData(
            pid=os.getpid(),
            build_id=build_id(),
            protocol_version=protocol_version(),
            socket=request.app.state.daemon_socket,
            port=getattr(request.app.state, "daemon_port", None),
            idle_exit=lifecycle.idle_exit,
            busy=await lifecycle.busy(),
            restart_pending=lifecycle.restart_pending,
            leases=[
                LeaseInfo(
                    id=lease.id,
                    client=lease.client,
                    pid=lease.pid,
                    opened_at=lease.opened_at,
                )
                for lease in lifecycle.leases()
            ],
        )
    )


@router.post(
    "/daemon/restart",
    response_model=DaemonControlResponse,
    operation_id="daemonRestart",
)
async def daemon_restart(
    request: Request, body: DaemonRestartRequest | None = None
) -> DaemonControlResponse:
    """Exit so the next client starts current code; never interrupts a turn."""
    lifecycle = lifecycle_of(request)
    if await lifecycle.busy():
        if not (body and body.when_idle):
            raise ApiError(
                409, "daemon_busy", "A turn is running; retry with whenIdle."
            )
        lifecycle.request_restart_when_idle()
        return _control("restart", "when_idle")
    # The watcher rechecks work, including admissions racing this request.
    lifecycle.request_restart_when_idle()
    return _control("restart", "now")


@router.post(
    "/daemon/stop", response_model=DaemonControlResponse, operation_id="daemonStop"
)
async def daemon_stop(
    request: Request, body: DaemonStopRequest | None = None
) -> DaemonControlResponse:
    lifecycle = lifecycle_of(request)
    if not (body and body.force) and await lifecycle.busy():
        raise ApiError(
            409, "daemon_busy", "A turn is running; pass force to stop anyway."
        )
    if body and body.force:
        _exit_after_response(lifecycle, "stop")
    else:
        lifecycle.request_stop_when_idle()
    return _control("stop", "now")


@router.get("/daemon/lease", operation_id="daemonLease")
async def daemon_lease(
    request: Request,
    client: str = Query(min_length=1, max_length=64),
    pid: int | None = Query(default=None, ge=1),
) -> StreamingResponse:
    """Hold the daemon up for as long as this stream stays open."""
    lifecycle = lifecycle_of(request)
    if lifecycle.exit_reason is not None:
        raise ApiError(503, "daemon_exiting", "The daemon is shutting down.")
    return StreamingResponse(
        _lease_stream(lifecycle, client, pid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _lease_stream(  # noqa: ANN202
    lifecycle: DaemonLifecycle, client: str, pid: int | None
):
    # Opened inside the body so the generator's finally always closes it.
    lease = lifecycle.open_lease(client, pid)
    try:
        payload = {"leaseID": lease.id, "pid": os.getpid(), "buildID": build_id()}
        yield f"event: daemon.lease\ndata: {json.dumps(payload)}\n\n"
        exiting = lifecycle.exiting
        while not exiting.is_set():
            try:
                await asyncio.wait_for(exiting.wait(), LEASE_HEARTBEAT_SECONDS)
            except TimeoutError:
                yield ": heartbeat\n\n"
        yield f"event: daemon.exiting\ndata: {json.dumps({'reason': lifecycle.exit_reason})}\n\n"
    finally:
        lifecycle.close_lease(lease)


def _exit_after_response(lifecycle: DaemonLifecycle, reason: str) -> None:
    asyncio.get_running_loop().call_later(0.05, lifecycle.request_exit, reason)


def _control(
    action: Literal["restart", "stop"], scheduled: Literal["now", "when_idle"]
) -> DaemonControlResponse:
    return DaemonControlResponse(
        data=DaemonControlData(action=action, scheduled=scheduled)
    )
