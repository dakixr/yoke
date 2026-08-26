"""MCP configuration and session-policy transport models."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from yoke.http.models.common import ApiModel
from yoke.http.models.common import LocationInfo


class McpToolInfo(ApiModel):
    name: str
    description: str | None = None
    input_schema: dict[str, object] | None = None


class McpServerInfo(ApiModel):
    name: str
    transport: str
    enabled: bool
    scope: Literal["global", "repo", "unknown"]
    source_path: str | None = None
    status: str = "configured"
    error: str | None = None
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] = Field(default_factory=list)
    tools: list[McpToolInfo] = Field(default_factory=list)
    truncated: bool = False


class McpListResponse(ApiModel):
    location: LocationInfo
    data: list[McpServerInfo]


class McpSessionPatchRequest(ApiModel):
    scope: Literal["session", "repo", "global"] = "session"
    enabled: bool | None = None
    enabled_tools: list[str] | None = None
    disabled_tools: list[str] | None = None


class McpSessionPatchResponse(ApiModel):
    data: McpServerInfo
