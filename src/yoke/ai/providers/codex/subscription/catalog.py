"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

from pathlib import Path
from typing import Any


from yoke.ai.providers.base import (
    ProviderModelInfo,
)
from yoke.ai.providers.model_selection import default_reasoning_effort_for_model

PROVIDER_NAME = "codex"

OAUTH_PROVIDER_ID = "openai-codex"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"
JWT_CLAIM_PATH = "https://api.openai.com/auth"
DEFAULT_BASE_URL = "https://chatgpt.com/backend-api"
DEFAULT_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
DEFAULT_CXAUTH_VAULT_NAME = ".codex-auth"
DEFAULT_LOGS_DIR = Path.home() / ".yoke" / "providers" / "logs"
DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS = 900.0
X_CODEX_TURN_STATE_HEADER = "x-codex-turn-state"
X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"
RESPONSES_LITE_MODEL_IDS = frozenset({"gpt-5.6-luna"})
DEFAULT_YOKE_ORIGINATOR = "yoke"
CODEX_CLI_ORIGINATOR = "codex_cli_rs"
MODEL_CATALOG = (
    ProviderModelInfo(
        id="gpt-5.6-sol",
        display_name="GPT-5.6 Sol",
        context_window_tokens=400_000,
        thinking_levels=("none", "low", "medium", "high", "xhigh", "max"),
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="gpt-5.6-terra",
        display_name="GPT-5.6 Terra",
        context_window_tokens=400_000,
        thinking_levels=("none", "low", "medium", "high", "xhigh", "max"),
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        context_window_tokens=400_000,
        thinking_levels=("none", "low", "medium", "high", "xhigh", "max"),
        default_thinking_level="medium",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="gpt-5.5",
        display_name="GPT-5.5",
        context_window_tokens=300_000,
        thinking_levels=("low", "medium", "high", "xhigh"),
        default_thinking_level="low",
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="gpt-5.4-mini",
        display_name="GPT-5.4 Mini",
        context_window_tokens=300_000,
        thinking_levels=("low", "medium", "high", "xhigh"),
        default_thinking_level="xhigh",
        supports_image_inputs=True,
    ),
)


def list_provider_models(context: Any) -> list[ProviderModelInfo]:
    del context
    return [model.model_copy(deep=True) for model in MODEL_CATALOG]


def default_reasoning_effort_for_model_id(model_id: str) -> str:
    for model in MODEL_CATALOG:
        if model.id == model_id.strip():
            return default_reasoning_effort_for_model(model) or "medium"
    return "medium"


def uses_responses_lite(model_id: str) -> bool:
    """Return whether a Codex model requires the Responses Lite request contract."""
    return model_id.strip() in RESPONSES_LITE_MODEL_IDS


def originator_for_model(model_id: str, configured_originator: str) -> str:
    """Use the backend-recognized Codex originator for Responses Lite models."""
    if (
        uses_responses_lite(model_id)
        and configured_originator == DEFAULT_YOKE_ORIGINATOR
    ):
        return CODEX_CLI_ORIGINATOR
    return configured_originator
