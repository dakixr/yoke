# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json

import httpx
import pytest

from yoke.agent.models import Message
from yoke.ai.providers.zai import ZAIConfig
from yoke.ai.providers.zai import ZAIProvider


def test_zai_catalog_exposes_documented_thinking_toggle() -> None:
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"))

    try:
        models = {model.id: model for model in provider.list_models()}
    finally:
        provider.close()

    assert models["glm-5.1"].thinking_levels == ("none", "thinking")
    assert models["glm-5.1"].default_thinking_level == "thinking"
    assert models["glm-5.2"].thinking_levels == ("none", "thinking")
    assert models["glm-5.2"].default_thinking_level == "thinking"


def test_zai_provider_sends_thinking_object_for_selected_effort() -> None:
    captured: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(
        ZAIConfig(ayoke_key="test", reasoning_effort="thinking"),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])

    assert captured["payload"] == {
        "model": "glm-5.1",
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "enabled"},
    }


def test_zai_provider_can_disable_thinking() -> None:
    captured: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(
        ZAIConfig(ayoke_key="test", reasoning_effort="none"),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])

    assert captured["payload"]["thinking"] == {"type": "disabled"}


def test_zai_set_model_rejects_unsupported_reasoning_effort() -> None:
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"))

    try:
        provider.set_model("glm-5.2", reasoning_effort="thinking")
        assert provider.config.model == "glm-5.2"
        assert provider.config.reasoning_effort == "thinking"

        with pytest.raises(ValueError, match="Unsupported reasoning effort"):
            provider.set_model("glm-5.2", reasoning_effort="high")
    finally:
        provider.close()
