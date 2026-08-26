"""Permission and question transport models for human-in-the-loop work."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from yoke.http.models.common import ApiModel


class PermissionRequestInfo(ApiModel):
    id: str
    session_id: str
    permission: str
    message: str
    created_at: str


class PermissionListResponse(ApiModel):
    data: list[PermissionRequestInfo]


class PermissionReplyRequest(ApiModel):
    reply: Literal["allow", "deny"]
    message: str | None = None


class PermissionResolution(ApiModel):
    request_id: str
    reply: Literal["allow", "deny"]
    message: str | None = None


class PermissionReplyResponse(ApiModel):
    data: PermissionResolution


class QuestionRequestInfo(ApiModel):
    id: str
    session_id: str
    question: str
    options: list[str] = Field(default_factory=list)
    multiple: bool = False
    created_at: str


class QuestionListResponse(ApiModel):
    data: list[QuestionRequestInfo]


class QuestionReplyRequest(ApiModel):
    answers: list[str] = Field(min_length=1)


class QuestionResolution(ApiModel):
    request_id: str
    answers: list[str] = Field(default_factory=list)
    rejected: bool = False


class QuestionReplyResponse(ApiModel):
    data: QuestionResolution
