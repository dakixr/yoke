# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from typing import cast

import httpx

from yoke.agent.models import Message
from yoke.ai.providers.base import fork_provider
from yoke.ai.providers.opencode_go import OpenCodeGoConfig
from yoke.ai.providers.opencode_go import OpenCodeGoProvider


def test_opencode_go_glm_flash_sends_selected_reasoning_effort() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["session"] = request.headers["x-opencode-session"]
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "glm-ok"}}]},
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="glm-5.3-flash",
            reasoning_effort="high",
            session_id="conversation-chat",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        message = provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["session"] == "conversation-chat"
    assert payload["model"] == "glm-5.3-flash"
    assert payload["reasoning_effort"] == "high"
    assert message.content == "glm-ok"


def test_opencode_go_luna_uses_responses_api() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["Authorization"]
        captured["session"] = request.headers["x-opencode-session"]
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
            session_id="conversation-responses",
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
    assert captured["session"] == "conversation-responses"
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


def test_opencode_go_generated_session_id_is_stable_across_requests() -> None:
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers["x-opencode-session"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="glm-5.3-flash"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        provider.complete([Message.user("first")], [])
        provider.complete([Message.user("second")], [])
    finally:
        provider.close()

    assert sessions == [provider.config.session_id, provider.config.session_id]
    assert provider.config.session_id


def test_opencode_go_session_id_survives_provider_fork() -> None:
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="glm-5.3-flash",
            session_id="conversation-fork",
        )
    )
    forked = cast(OpenCodeGoProvider, fork_provider(provider))

    try:
        assert forked is not provider
        assert forked.config.session_id == "conversation-fork"
    finally:
        forked.close()
        provider.close()


def test_opencode_go_can_rebind_session_id() -> None:
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers["x-opencode-session"])
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="glm-5.3-flash",
            session_id="source-session",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        provider.complete([Message.user("source")], [])
        provider.set_session_id("fork-session")
        provider.complete([Message.user("fork")], [])
    finally:
        provider.close()

    assert sessions == ["source-session", "fork-session"]
