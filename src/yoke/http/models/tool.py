"""Tool inventory and session enablement transport models."""

from __future__ import annotations

from pydantic import Field

from yoke.http.models.common import ApiModel
from yoke.http.models.common import LocationInfo


class ToolInfo(ApiModel):
    name: str
    description: str
    enabled: bool
    source: str
    source_path: str | None = None
    capability_id: str | None = None
    input_schema: dict[str, object] = Field(default_factory=dict)


class ToolListResponse(ApiModel):
    location: LocationInfo
    data: list[ToolInfo]


class ToolPatchRequest(ApiModel):
    enabled: list[str] = Field(default_factory=list)
    disabled: list[str] = Field(default_factory=list)


class ToolPatchData(ApiModel):
    enabled: list[str]


class ToolPatchResponse(ApiModel):
    data: ToolPatchData
