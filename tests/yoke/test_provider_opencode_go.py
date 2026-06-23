# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from typing import cast

import httpx

from yoke.ai.providers.opencode_go import OpenCodeGoConfig
from yoke.ai.providers.opencode_go import OpenCodeGoProvider


def _payload_messages(captured: dict[str, object]) -> list[dict[str, object]]:
    payload = cast(dict[str, object], captured["payload"])
    return cast(list[dict[str, object]], payload["messages"])


def _msg_content(msg: dict[str, object]) -> list[dict[str, object]]:
    return cast(list[dict[str, object]], msg["content"])


def _anthropic_handler(captured: dict[str, object], response_json: dict[str, object]):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=response_json)

    return handler


def test_opencode_go_catalog_excludes_deprecated_models() -> None:
    provider = OpenCodeGoProvider(OpenCodeGoConfig(api_key="test"))
    try:
        model_ids = {model.id for model in provider.list_models()}
        assert "glm-5.2" in model_ids
        assert "kimi-k2.7-code" in model_ids
        assert "deepseek-v4-pro" in model_ids
        assert "deepseek-v4-flash" in model_ids
        assert (
            not {
                "glm-5.1",
                "glm-5",
                "kimi-k2.6",
                "kimi-k2.5",
                "mimo-v2.5",
                "mimo-v2-omni",
                "mimo-v2-pro",
                "mimo-v2.5-pro",
                "minimax-m3",
                "minimax-m2.7",
                "minimax-m2.5",
                "qwen3.7-max",
                "qwen3.7-plus",
                "qwen3.6-plus",
                "qwen3.5-plus",
            }
            & model_ids
        )
    finally:
        provider.close()
