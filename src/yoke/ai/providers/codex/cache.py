"""Prompt-cache affinity helpers for Codex providers."""

from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Protocol


class PromptCacheConfig(Protocol):
    auth_path: Path
    accounts_dir: Path
    base_url: str
    model: str
    prompt_cache_key: str | None


def build_prompt_cache_key(config: PromptCacheConfig) -> str:
    """Return a session-stable key or a provider-instance fallback key."""
    if config.prompt_cache_key:
        seed = f"yoke-session\0{config.prompt_cache_key}"
    else:
        seed = "\0".join(
            (
                str(config.auth_path.expanduser()),
                str(config.accounts_dir.expanduser()),
                config.base_url,
                config.model,
                secrets.token_hex(16),
            )
        )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()
