# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from typing import cast

import httpx
import pytest

from yoke.agent.models import Message
from yoke.ai.providers.opencode_go import OpenCodeGoConfig
from yoke.ai.providers.opencode_go import OpenCodeGoProvider


def test_opencode_go_catalog_excludes_deprecated_models() -> None:
    provider = OpenCodeGoProvider(OpenCodeGoConfig(api_key="test"))
    try:
        models = {model.id: model for model in provider.list_models()}
        model_ids = set(models)
        assert "gpt-5.6-luna" in model_ids
        assert "glm-5.2" in model_ids
        assert "glm-5.3" in model_ids
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
        assert models["glm-5.2"].thinking_levels == ("none", "high", "max")
        assert models["glm-5.2"].default_thinking_level == "max"
        assert models["glm-5.3"].thinking_levels == ("low", "high", "max")
        assert models["glm-5.3"].default_thinking_level == "max"
    finally:
        provider.close()


@pytest.mark.parametrize("model", ["glm-5.2", "glm-5.3"])
def test_opencode_go_glm_sends_selected_reasoning_effort(model: str) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "glm-ok"}}]},
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model=model,
            reasoning_effort="high",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        message = provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert payload["model"] == model
    assert payload["reasoning_effort"] == "high"
    assert message.content == "glm-ok"


def test_opencode_go_luna_uses_responses_api() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "luna-ok"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "read_file",
                        "arguments": '{"path":"README.md"}',
                    },
                ],
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 7,
                    "total_tokens": 19,
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="secret",
            model="gpt-5.6-luna",
            reasoning_effort="medium",
        ),
        http_client=client,
    )
    tools: list[dict[str, object]] = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file",
                "parameters": {"type": "object"},
            },
        }
    ]

    try:
        message = provider.complete(
            [Message.system("Be concise"), Message.user("Probe Luna")], tools
        )
    finally:
        provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert captured["url"] == "https://opencode.ai/zen/go/v1/responses"
    assert captured["authorization"] == "Bearer secret"
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["instructions"] == "Be concise"
    assert payload["input"] == [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": "Probe Luna"}],
        }
    ]
    assert payload["parallel_tool_calls"] is False
    assert payload["reasoning"] == {
        "effort": "medium",
        "summary": "auto",
        "context": "all_turns",
    }
    assert payload["max_output_tokens"] == 65_536
    assert message.content == "luna-ok"
    assert message.tool_calls[0].id == "call_123"
    assert message.tool_calls[0].function.name == "read_file"
    assert message.usage is not None
    assert message.usage.total_tokens == 19
