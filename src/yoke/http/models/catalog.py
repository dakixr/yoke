"""Provider and model catalog transport models."""

from __future__ import annotations

from yoke.http.models.common import ApiModel
from yoke.http.models.common import LocationInfo


class ProviderInfo(ApiModel):
    id: str
    ready: bool
    reason: str | None = None
    current_model: str | None = None
    current_reasoning_effort: str | None = None


class ProviderListResponse(ApiModel):
    location: LocationInfo
    data: list[ProviderInfo]


class ModelCapabilities(ApiModel):
    images: bool | None = None
    tools: bool = True


class ModelInfo(ApiModel):
    id: str
    provider: str
    name: str
    reasoning_efforts: list[str]
    capabilities: ModelCapabilities
    context_window_tokens: int


class ModelListResponse(ApiModel):
    location: LocationInfo
    data: list[ModelInfo]
