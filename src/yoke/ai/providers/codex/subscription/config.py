"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, Field

from .catalog import (
    DEFAULT_BASE_URL,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
)


def resolve_codex_logs_dir(
    env: Mapping[str, str] | None = None,
    *,
    include_websocket_override: bool = False,
) -> Path:
    """Resolve the Codex log directory from the current environment and home."""
    source = os.environ if env is None else env
    configured = source.get("YOKE_CODEX_LOGS_DIR")
    if not configured and include_websocket_override:
        configured = source.get("YOKE_CODEX_WEBSOCKETS_LOGS_DIR")
    configured = configured or source.get("YOKE_PROVIDER_LOGS_DIR")
    if configured:
        return Path(configured)
    return Path.home() / ".yoke" / "providers" / "logs"


class CodexSubscriptionConfig(BaseModel):
    auth_path: Path
    accounts_dir: Path
    auths_path: Path
    selection_path: Path
    selection_ttl_seconds: int = 1800
    model: str = "gpt-5.6-sol"
    prompt_cache_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    originator: str = "yoke"
    timeout_seconds: float = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 15.0
    reasoning_effort: str = "medium"
    text_verbosity: str = "medium"
    logs_dir: Path = Field(default_factory=resolve_codex_logs_dir)
