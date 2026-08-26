"""Location-contained filesystem routes."""

from __future__ import annotations

import mimetypes
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Query
from fastapi import Request
from fastapi.responses import FileResponse

from yoke.http.auth import require_auth
from yoke.http.models.filesystem import FileListResponse
from yoke.http.services.filesystem_service import FilesystemService


router = APIRouter(dependencies=[Depends(require_auth)])


def _service(request: Request) -> FilesystemService:
    return request.app.state.filesystem_service


@router.get("/fs/list", response_model=FileListResponse, operation_id="listFiles")
def list_files(
    request: Request,
    directory: str | None = Query(default=None),
    path: str | None = Query(default=None),
) -> FileListResponse:
    return _service(request).list(directory=directory, path=path)


@router.get("/fs/find", response_model=FileListResponse, operation_id="findFiles")
def find_files(
    request: Request,
    directory: str | None = Query(default=None),
    query: str = Query(min_length=1),
    type: Literal["file", "directory"] = Query(default="file"),
    limit: int = Query(default=50, ge=1, le=200),
) -> FileListResponse:
    return _service(request).find(
        directory=directory,
        query=query,
        entry_type=type,
        limit=limit,
    )


@router.get("/fs/read", operation_id="readFile")
def read_file(
    request: Request,
    directory: str | None = Query(default=None),
    path: str = Query(min_length=1),
) -> FileResponse:
    _root, target = _service(request).readable_file(directory=directory, path=path)
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{target.name}"'},
    )
