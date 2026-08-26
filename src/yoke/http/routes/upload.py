"""Prompt attachment upload route."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Query
from fastapi import Request
from fastapi import UploadFile

from yoke.http.auth import require_auth
from yoke.http.models.upload import UploadResponse
from yoke.http.services.upload_service import UploadService


router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/upload", response_model=UploadResponse, operation_id="uploadAttachment")
async def upload_attachment(
    request: Request,
    file: UploadFile = File(),
    session_id: str | None = Query(default=None, alias="sessionID"),
    purpose: Literal["promptAttachment"] = Query(default="promptAttachment"),
) -> UploadResponse:
    del purpose
    service: UploadService = request.app.state.upload_service
    return UploadResponse(data=await service.create(file, session_id=session_id))
