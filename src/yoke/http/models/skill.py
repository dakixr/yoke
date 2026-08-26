"""Skill catalog and activation transport models."""

from __future__ import annotations

from yoke.http.models.common import ApiModel
from yoke.http.models.common import LocationInfo


class SkillInfo(ApiModel):
    name: str
    description: str
    source_path: str
    active: bool = False


class SkillListResponse(ApiModel):
    location: LocationInfo
    data: list[SkillInfo]


class SessionSkillData(ApiModel):
    active: list[SkillInfo]
    available: list[SkillInfo]


class SessionSkillResponse(ApiModel):
    data: SessionSkillData


class SkillActivateRequest(ApiModel):
    prompt: str | None = None


class SkillActivateData(ApiModel):
    activated: SkillInfo
    prompt_input_id: str | None = None


class SkillActivateResponse(ApiModel):
    data: SkillActivateData
