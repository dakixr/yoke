"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .catalog import (
    DEFAULT_BASE_URL,
    DEFAULT_CXAUTH_VAULT_NAME,
    DEFAULT_LOGS_DIR,
    DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS,
    DEFAULT_YOKE_ORIGINATOR,
    default_reasoning_effort_for_model_id,
)
from .config import CodexSubscriptionConfig
from .provider import CodexSubscriptionProvider


def register_provider(context: Any) -> CodexSubscriptionProvider:
    env = os.environ if context.env is None else context.env
    cxauth_vault = context.home / DEFAULT_CXAUTH_VAULT_NAME
    return CodexSubscriptionProvider(
        CodexSubscriptionConfig(
            auth_path=context.home / ".codex" / "auth.json",
            accounts_dir=cxauth_vault / "accounts",
            auths_path=(
                Path(env.get("YOKE_CODEX_AUTHS_PATH", ""))
                if env.get("YOKE_CODEX_AUTHS_PATH")
                else context.home / ".yoke" / "providers" / "codex-auth" / "auths.json"
            ),
            selection_path=(
                Path(env.get("YOKE_CODEX_SELECTION_PATH", ""))
                if env.get("YOKE_CODEX_SELECTION_PATH")
                else context.home
                / ".yoke"
                / "providers"
                / "codex-auth"
                / "selection.json"
            ),
            selection_ttl_seconds=int(
                env.get("YOKE_CODEX_SELECTION_TTL_SECONDS") or "1800"
            ),
            model=(context.model or env.get("YOKE_CODEX_MODEL") or "gpt-5.6-sol"),
            prompt_cache_key=getattr(context, "session_id", None),
            base_url=(env.get("YOKE_CODEX_BASE_URL") or DEFAULT_BASE_URL),
            originator=env.get("YOKE_CODEX_ORIGINATOR") or DEFAULT_YOKE_ORIGINATOR,
            timeout_seconds=float(
                env.get("YOKE_CODEX_TIMEOUT_SECONDS")
                or str(DEFAULT_STREAM_IDLE_TIMEOUT_SECONDS)
            ),
            max_retries=int(env.get("YOKE_CODEX_MAX_RETRIES") or "5"),
            reasoning_effort=(
                context.reasoning_effort
                or env.get("YOKE_CODEX_REASONING_EFFORT")
                or default_reasoning_effort_for_model_id(
                    context.model or env.get("YOKE_CODEX_MODEL") or "gpt-5.6-sol"
                )
            ),
            text_verbosity=(env.get("YOKE_CODEX_TEXT_VERBOSITY") or "medium"),
            logs_dir=Path(
                env.get("YOKE_CODEX_LOGS_DIR")
                or env.get("YOKE_PROVIDER_LOGS_DIR")
                or str(DEFAULT_LOGS_DIR)
            ),
        )
    )
