# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from typing import cast

import httpx

from yoke.agent.models import Message
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


def test_opencode_go_anthropic_sends_interleaved_thinking_beta_header() -> None:
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {"content": [{"type": "text", "text": "done"}], "usage": {}},
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="qwen3.7-max"),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])
    provider.close()

    headers = cast(dict[str, str], captured["headers"])
    assert "interleaved-thinking-2025-05-14" in headers["anthropic-beta"]
    assert "fine-grained-tool-streaming-2025-05-14" in headers["anthropic-beta"]


def test_opencode_go_anthropic_parses_thinking_block_from_response() -> None:
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Let me reason.",
                    "signature": "sig123",
                },
                {"type": "text", "text": "The answer is 42."},
            ],
            "usage": {},
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="qwen3.7-max"),
        http_client=client,
    )

    message = provider.complete([Message.user("hello")], [])
    provider.close()

    assert message.reasoning_content == "Let me reason."
    assert message.reasoning_signature == "sig123"
    assert message.content == "The answer is 42."


def test_opencode_go_anthropic_round_trips_thinking_block_on_next_request() -> None:
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "prior reasoning",
                    "signature": "sig_abc",
                },
                {"type": "text", "text": "done"},
            ],
            "usage": {},
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="qwen3.7-max"),
        http_client=client,
    )

    first = provider.complete([Message.user("hello")], [])
    provider.complete([Message.user("hello"), first, Message.user("again")], [])
    provider.close()

    messages = _payload_messages(captured)
    assistant_blocks = _msg_content(messages[1])
    thinking_block = assistant_blocks[0]
    assert thinking_block["type"] == "thinking"
    assert thinking_block["thinking"] == "prior reasoning"
    assert thinking_block["signature"] == "sig_abc"


def test_opencode_go_anthropic_round_trips_thinking_with_tool_use() -> None:
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {
            "content": [
                {
                    "type": "thinking",
                    "thinking": "I need a tool.",
                    "signature": "sig_xyz",
                },
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "read",
                    "input": {"path": "README.md"},
                },
            ],
            "usage": {},
        },
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="qwen3.7-plus"),
        http_client=client,
    )

    first = provider.complete([Message.user("read the file")], [])
    provider.complete(
        [Message.user("read the file"), first, Message.tool("call_1", "ok")],
        [],
    )
    provider.close()

    messages = _payload_messages(captured)
    assistant_blocks = _msg_content(messages[1])
    assert assistant_blocks[0]["type"] == "thinking"
    assert assistant_blocks[0]["signature"] == "sig_xyz"
    assert assistant_blocks[1]["type"] == "tool_use"
    assert assistant_blocks[1]["id"] == "call_1"


def test_opencode_go_anthropic_thinking_config_high() -> None:
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {"content": [{"type": "text", "text": "done"}], "usage": {}},
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="qwen3.7-max",
            reasoning_effort="high",
        ),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])
    provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16_000}


def test_opencode_go_anthropic_thinking_config_max() -> None:
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {"content": [{"type": "text", "text": "done"}], "usage": {}},
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="qwen3.7-max",
            reasoning_effort="max",
        ),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])
    provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 31_999}


def test_opencode_go_minimax_m3_thinking_uses_adaptive() -> None:
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {"content": [{"type": "text", "text": "done"}], "usage": {}},
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="minimax-m3",
            reasoning_effort="thinking",
        ),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])
    provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert payload["thinking"] == {"type": "adaptive"}


def test_opencode_go_qwen_models_now_advertise_thinking_levels() -> None:
    provider = OpenCodeGoProvider(OpenCodeGoConfig(api_key="test"))
    try:
        models = {m.id: m for m in provider.list_models()}
        assert models["qwen3.7-max"].thinking_levels == ("high", "max")
        assert models["qwen3.7-plus"].thinking_levels == ("high", "max")
        assert models["qwen3.6-plus"].thinking_levels == ("high", "max")
        assert models["qwen3.5-plus"].thinking_levels == ("high", "max")
        assert models["minimax-m2.7"].thinking_levels == ("high", "max")
    finally:
        provider.close()


def test_opencode_go_anthropic_no_thinking_block_without_signature() -> None:
    """Messages with reasoning_content but no signature must not emit thinking blocks."""
    captured: dict[str, object] = {}
    handler = _anthropic_handler(
        captured,
        {"content": [{"type": "text", "text": "done"}], "usage": {}},
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="qwen3.7-max"),
        http_client=client,
    )

    prior = Message(
        role="assistant",
        content="ok",
        reasoning_content="some reasoning",
        reasoning_signature=None,
    )
    provider.complete([Message.user("hello"), prior], [])
    provider.close()

    messages = _payload_messages(captured)
    assistant_blocks = _msg_content(messages[1])
    assert not any(b["type"] == "thinking" for b in assistant_blocks)
