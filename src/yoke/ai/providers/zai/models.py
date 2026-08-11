"""Configuration, catalog, and response models for Z.AI."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from yoke.agent.models import Message, MessagePhase, Role, ToolCall
from yoke.ai.providers.base import ProviderModelInfo

PROVIDER_NAME = "zai"
THINKING_LEVELS = ("none", "thinking")
MODEL_CATALOG = (
    ProviderModelInfo(
        id="glm-5.2",
        display_name="GLM-5.2",
        context_window_tokens=1_000_000,
        thinking_levels=THINKING_LEVELS,
        default_thinking_level="thinking",
        supports_image_inputs=False,
    ),
)


def list_provider_models(context):
    del context
    return [model.model_copy(deep=True) for model in MODEL_CATALOG]


def register_provider(context):
    env = context.env or {}
    api_key = env.get("ZAI_API_KEY", "")
    if not api_key:
        raise ValueError("zai provider requires ZAI_API_KEY.")
    from yoke.ai.providers.zai.provider import ZAIProvider

    return ZAIProvider(
        ZAIConfig(
            api_key=api_key,
            model=context.model or "glm-5.2",
            reasoning_effort=context.reasoning_effort,
            debug_log_path=env.get("ZAI_DEBUG_LOG_PATH") or None,
        )
    )


class ZAIConfig(BaseModel):
    """Configuration for the native Z.AI coding provider."""

    api_key: str
    model: str = "glm-5.2"
    # This key is for the Z.AI Coding Plan; the regular paas endpoint can
    # reject it even when the token is valid for coding-plan traffic.
    base_url: str = "https://api.z.ai/api/coding/paas/v4"
    debug_log_path: str | None = None
    reasoning_effort: str | None = None
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 32.0
    connect_timeout_seconds: float = 10.0
    read_idle_timeout_seconds: float = 900.0
    total_timeout_seconds: float = 900.0


class ZAIResponseMessage(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    phase: MessagePhase | None = None
    reasoning_content: str | None = None

    @field_validator("phase", mode="before")
    @classmethod
    def normalize_phase(cls, value: object) -> MessagePhase | None:
        if not isinstance(value, str):
            return None
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"commentary", "preamble"}:
            return "commentary"
        if normalized in {"final_answer", "final"}:
            return "final_answer"
        return None

    def to_message(self) -> Message:
        return Message(
            role=self.role,
            content=self.content,
            tool_calls=self.tool_calls,
            phase=self.phase,
            reasoning_content=self.reasoning_content,
        )


class ZAIChoice(BaseModel):
    message: ZAIResponseMessage


class ZAIChatCompletionResponse(BaseModel):
    choices: list[ZAIChoice]
    usage: dict[str, object] | None = None


def _thinking_config(reasoning_effort: str | None) -> dict[str, object] | None:
    if reasoning_effort is None:
        return None
    normalized = reasoning_effort.strip().lower()
    if normalized == "none":
        return {"type": "disabled"}
    if normalized == "thinking":
        return {"type": "enabled", "clear_thinking": True}
    return None
