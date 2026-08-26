"""Slash-command palette transport models."""

from __future__ import annotations

from yoke.http.models.common import ApiModel


class CommandInfo(ApiModel):
    name: str
    description: str
    usage: str | None = None
    action: str


class CommandListResponse(ApiModel):
    data: list[CommandInfo]
