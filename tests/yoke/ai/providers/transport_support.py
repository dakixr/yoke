"""Deterministic provider construction and HTTP responses for transport tests."""

# ruff: noqa: S101
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionConfig,
    CodexSubscriptionProvider,
    OAuthCredentials,
)
from yoke.ai.providers.codex.websocket.config import CodexConfig
from yoke.ai.providers.codex.websocket.provider import CodexProvider
from yoke.ai.providers.openai_compat import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from yoke.ai.providers.opencode_go import OpenCodeGoConfig, OpenCodeGoProvider
from yoke.ai.providers.zai import ZAIConfig, ZAIProvider

HTTP_KINDS = ("openai", "zai", "go-chat", "go-responses", "codex-http")


def _provider(
    kind: str,
    tmp_path: Path,
    client: httpx.Client | None,
    sleep: Callable[[float], None] = lambda _s: None,
) -> Any:
    common = {"http_client": client, "sleep": sleep}
    if kind == "openai":
        return OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key="test", model="test", max_retries=1, retry_backoff_seconds=0
            ),
            **common,
        )
    if kind == "zai":
        return ZAIProvider(
            ZAIConfig(api_key="test", max_retries=1, retry_backoff_seconds=0), **common
        )
    if kind.startswith("go-"):
        return OpenCodeGoProvider(
            OpenCodeGoConfig(
                api_key="test",
                model="deepseek-v4.1-flash"
                if kind == "go-chat"
                else "muse-spark-1.3-contributor",
                max_retries=1,
                retry_backoff_seconds=0,
            ),
            **common,
        )
    auth: dict[str, Any] = {
        "auth_path": tmp_path / "auth",
        "accounts_dir": tmp_path / "accounts",
        "auths_path": tmp_path / "auths",
        "selection_path": tmp_path / "selection",
        "logs_dir": tmp_path / "logs",
        "prompt_cache_key": "cache-fixed",
        "max_retries": 1,
        "retry_backoff_seconds": 0,
    }
    if kind == "codex-ws":
        return CodexProvider(CodexConfig(**auth), sleep=sleep)
    return CodexSubscriptionProvider(CodexSubscriptionConfig(**auth), **common)


def _response(kind: str) -> httpx.Response:
    if kind == "openai":
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )
    if kind == "go-responses":
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            },
        )
    if kind == "codex-http":
        event = {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "ok"}],
                    }
                ]
            },
        }
    else:
        event = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
    return httpx.Response(
        200,
        content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
        headers={"content-type": "text/event-stream"},
    )


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        CodexSubscriptionProvider,
        "_fresh_credentials",
        lambda _self: OAuthCredentials(
            access="test", refresh="test", expires=4102444800000, account_id="acct"
        ),
    )
    monkeypatch.setattr(
        CodexSubscriptionProvider, "_log_event", lambda *_args, **_kwargs: None
    )
