"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from .catalog import (
    DEFAULT_BASE_URL,
    DEFAULT_LOGS_DIR,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
)


class CodexSubscriptionConfig(BaseModel):
    auth_path: Path
    accounts_dir: Path
    auths_path: Path
    selection_path: Path
    selection_ttl_seconds: int = 1800
    model: str = "gpt-5.5"
    prompt_cache_key: str | None = None
    base_url: str = DEFAULT_BASE_URL
    originator: str = "yoke"
    timeout_seconds: float = DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    max_retry_backoff_seconds: float = 15.0
    reasoning_effort: str = "medium"
    text_verbosity: str = "medium"
    logs_dir: Path = DEFAULT_LOGS_DIR
