"""OpenCode Go model catalog and configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, field_validator

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import cloned_model_catalog
from yoke.ai.providers.openai_compat import build_model_catalog

if TYPE_CHECKING:
    from yoke.ai.providers.opencode_go.provider import OpenCodeGoProvider

PROVIDER_NAME = "opencode-go"
ENV_API_KEY = "OPENCODE_API_KEY"
OPENAI_BASE_URL = "https://opencode.ai/zen/go/v1"

DEEPSEEK_THINKING_LEVELS = ("high", "max")
GLM_THINKING_LEVELS = ("none", "high", "max")
GROK_THINKING_LEVELS = ()
KIMI_THINKING_LEVELS = ()
LUNA_THINKING_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")

MODEL_PROTOCOLS = {
    "gpt-5.6-luna": "responses",
    "glm-5.2": "openai",
    "deepseek-v4-flash": "openai",
    "grok-4.5": "openai",
    "kimi-k2.7-code": "openai",
    "deepseek-v4-pro": "openai",
}

MODEL_CATALOG = build_model_catalog(
    ProviderModelInfo(
        id="gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        context_window_tokens=400_000,
        thinking_levels=LUNA_THINKING_LEVELS,
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="glm-5.2",
        display_name="GLM-5.2",
        context_window_tokens=400_000,
        thinking_levels=GLM_THINKING_LEVELS,
        default_thinking_level="max",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="grok-4.5",
        display_name="Grok 4.5",
        context_window_tokens=400_000,
        thinking_levels=GROK_THINKING_LEVELS,
        default_thinking_level=None,
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="kimi-k3",
        display_name="Kimi K3",
        context_window_tokens=400_000,
        thinking_levels=KIMI_THINKING_LEVELS,
        default_thinking_level=None,
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="kimi-k2.7-code",
        display_name="Kimi K2.7 Code",
        context_window_tokens=262_144,
        thinking_levels=KIMI_THINKING_LEVELS,
        default_thinking_level=None,
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="deepseek-v4-pro",
        display_name="DeepSeek V4 Pro",
        context_window_tokens=1_000_000,
        thinking_levels=DEEPSEEK_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=False,
    ),
    ProviderModelInfo(
        id="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        context_window_tokens=1_000_000,
        thinking_levels=DEEPSEEK_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=False,
    ),
)

OPENCODE_GO_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "thinking",
)


def list_provider_models(context: Any) -> list[ProviderModelInfo]:
    del context
    return cloned_model_catalog(MODEL_CATALOG)


def register_provider(context: Any) -> OpenCodeGoProvider:
    env = os.environ if context.env is None else context.env
    api_key = env.get(ENV_API_KEY, "").strip()
    if not api_key:
        raise ValueError(
            f"OpenCode Go API key not found. Please provide it via {ENV_API_KEY} environment variable."
        )
    from yoke.ai.providers.opencode_go.provider import OpenCodeGoProvider

    return OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key=api_key,
            model=_normalize_model_id(context.model or "kimi-k2.7-code"),
            timeout_seconds=float(env.get("YOKE_OPENCODE_GO_TIMEOUT_SECONDS") or "900"),
            max_retries=int(env.get("YOKE_OPENCODE_GO_MAX_RETRIES") or "5"),
            reasoning_effort=(
                context.reasoning_effort
                or env.get("YOKE_OPENCODE_GO_REASONING_EFFORT")
                or None
            ),
        )
    )


class OpenCodeGoConfig(BaseModel):
    api_key: str
    model: str = "kimi-k2.7-code"
    timeout_seconds: float = 900.0
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 15.0
    reasoning_effort: str | None = None
    model_catalog: tuple[ProviderModelInfo, ...] = MODEL_CATALOG

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str) -> str:
        return _normalize_model_id(value)

    @field_validator("reasoning_effort")
    @classmethod
    def validate_reasoning_effort(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized not in OPENCODE_GO_REASONING_EFFORTS:
            raise ValueError(
                "reasoning_effort must be one of none, minimal, low, "
                "medium, high, xhigh, max, or thinking"
            )
        return normalized


def _normalize_model_id(model_id: str) -> str:
    normalized = model_id.strip()
    prefix = f"{PROVIDER_NAME}/"
    if normalized.startswith(prefix):
        normalized = normalized[len(prefix) :]
    return normalized


def _max_output_tokens(model_id: str) -> int:
    del model_id
    return 65_536
