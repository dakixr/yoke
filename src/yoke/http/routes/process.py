"""Runtime process inspector routes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request

from yoke.http.auth import require_auth
from yoke.http.models.process import ProcessListResponse
from yoke.http.models.process import ProcessOutputResponse
from yoke.http.models.process import ProcessResponse
from yoke.http.models.process import ProcessSignalRequest
from yoke.http.models.process import ProcessStdinRequest
from yoke.http.services.process_service import ProcessService


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> ProcessService:
    return request.app.state.process_service


@router.get("/process", response_model=ProcessListResponse, operation_id="listProcesses")
def list_processes(
    request: Request,
    session_id: str | None = Query(default=None, alias="sessionID"),
    status: Literal["running", "exited", "failed"] | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
) -> ProcessListResponse:
    return _service(request).list_processes(
        session_id=session_id,
        status=status,
        limit=limit,
    )


@router.get(
    "/process/{process_id}",
    response_model=ProcessResponse,
    operation_id="getProcess",
)
def get_process(request: Request, process_id: str) -> ProcessResponse:
    return _service(request).process(process_id)


@router.get(
    "/process/{process_id}/output",
    response_model=ProcessOutputResponse,
    operation_id="getProcessOutput",
)
def get_process_output(
    request: Request,
    process_id: str,
    after_seq: int = Query(default=0, alias="afterSeq", ge=0),
    limit: int = Query(default=200, ge=1, le=500),
) -> ProcessOutputResponse:
    return _service(request).output(process_id, after_seq=after_seq, limit=limit)


@router.post(
    "/process/{process_id}/stdin",
    response_model=ProcessResponse,
    operation_id="writeProcessStdin",
)
def write_process_stdin(
    request: Request,
    process_id: str,
    body: ProcessStdinRequest,
) -> ProcessResponse:
    return _service(request).write_stdin(process_id, body.text)


@router.post(
    "/process/{process_id}/signal",
    response_model=ProcessResponse,
    operation_id="signalProcess",
)
def signal_process(
    request: Request,
    process_id: str,
    body: ProcessSignalRequest,
) -> ProcessResponse:
    return _service(request).signal(process_id, body.signal)
