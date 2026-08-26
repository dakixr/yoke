"""Upload transport models."""

from __future__ import annotations

from typing import Literal

from yoke.http.models.common import ApiModel


class UploadInfo(ApiModel):
    id: str
    uri: str
    name: str
    mime: str
    size: int
    expires_at: str


class UploadResponse(ApiModel):
    data: UploadInfo


class UploadPurpose(ApiModel):
    purpose: Literal["promptAttachment"] = "promptAttachment"
