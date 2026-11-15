"""OpenCode Go model catalog and configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import cloned_model_catalog
from yoke.ai.providers.openai_compat import build_model_catalog

if TYPE_CHECKING:
    from yoke.ai.providers.opencode_go.provider import OpenCodeGoProvider

PROVIDER_NAME = "opencode-go"
ENV_API_KEY = "OPENCODE_API_KEY"
DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
ENV_BASE_URL = "YOKE_OPENCODE_GO_BASE_URL"
ENV_MODEL_PREFIX = "YOKE_OPENCODE_GO_MODEL_PREFIX"

DEEPSEEK_41_THINKING_LEVELS = ("low", "high", "max")
MUSE_SPARK_THINKING_LEVELS = ("minimal", "low", "medium", "high", "xhigh")
DEFAULT_MODEL_ID = "deepseek-v4.1-flash"

MODEL_PROTOCOLS = {
    "muse-spark-1.3-contributor": "responses",
    "deepseek-v4.1-flash": "openai",
}

MODEL_CATALOG = build_model_catalog(
    ProviderModelInfo(
        id="muse-spark-1.3-contributor",
        display_name="Muse Spark 1.3 Contributor",
        context_window_tokens=400_000,
        thinking_levels=MUSE_SPARK_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="deepseek-v4.1-flash",
        display_name="DeepSeek V4.1 Flash",
        context_window_tokens=400_000,
        thinking_levels=DEEPSEEK_41_THINKING_LEVELS,
        default_thinking_level="high",
        supports_image_inputs=True,
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
            model=_normalize_model_id(context.model or DEFAULT_MODEL_ID),
            session_id=context.session_id or uuid4().hex,
            base_url=env.get(ENV_BASE_URL, "").strip() or DEFAULT_BASE_URL,
            model_prefix=env.get(ENV_MODEL_PREFIX, "").strip(),
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
    model: str = DEFAULT_MODEL_ID
    session_id: str = Field(default_factory=lambda: uuid4().hex)
    # A gateway such as CLIProxy can serve Go models under its own base URL
    # and a namespaced upstream ID ("opencode-go/<id>"). Yoke's own model IDs
    # stay unprefixed, so sessions and selections are unaffected.
    base_url: str = DEFAULT_BASE_URL
    model_prefix: str = ""
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

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith(("http://", "https://")):
            raise ValueError("base_url must be an http(s) URL")
        return normalized

    @field_validator("model_prefix")
    @classmethod
    def validate_model_prefix(cls, value: str) -> str:
        return value.strip().strip("/")

    def upstream_model(self, model_id: str) -> str:
        """Return the model ID sent on the wire."""
        return f"{self.model_prefix}/{model_id}" if self.model_prefix else model_id

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        return normalized

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
