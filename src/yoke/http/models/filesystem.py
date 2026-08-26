"""Filesystem discovery transport models."""

from __future__ import annotations

from typing import Literal

from yoke.http.models.common import ApiModel
from yoke.http.models.common import LocationInfo


class FileEntry(ApiModel):
    name: str
    path: str
    type: Literal["file", "directory"]
    size: int | None = None


class FileListResponse(ApiModel):
    location: LocationInfo
    data: list[FileEntry]
