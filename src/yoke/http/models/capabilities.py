"""Health and server-capability transport models."""

from __future__ import annotations

from yoke.http.models.common import ApiModel


class HealthData(ApiModel):
    healthy: bool
    version: str
    protocol_version: str


class HealthResponse(ApiModel):
    data: HealthData


class FeatureFlags(ApiModel):
    global_events: bool = True
    prompt_admission: bool = True
    steering: bool = True
    queue_editor: bool = True
    session_tree: bool = True
    tool_inspector: bool = True
    process_inspector: bool = True
    pty: bool = False
    permissions: bool = True
    questions: bool = True
    mcp: bool = True
    skills: bool = True
    images: bool = True
    session_archive: bool = True
    session_title_regeneration: bool = True


class CapabilityLimits(ApiModel):
    max_prompt_bytes: int = 1_048_576
    max_attachment_bytes: int = 20_971_520
    history_page_max: int = 200


class CapabilitiesData(ApiModel):
    protocol_version: str = "1"
    features: FeatureFlags
    limits: CapabilityLimits


class CapabilitiesResponse(ApiModel):
    data: CapabilitiesData
