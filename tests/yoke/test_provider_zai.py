# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.ai.providers.zai import ZAIConfig
from yoke.ai.providers.zai import ZAIProvider
from yoke.ai.providers.zai import register_provider


def _sse_response(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    tool_call: dict[str, object] | None = None,
    finish_reason: str = "stop",
) -> httpx.Response:
    """Build a minimal SSE chat-completion stream response for tests."""
    chunks = []
    if reasoning_content is not None:
        chunks.append(
            {"choices": [{"delta": {"reasoning_content": reasoning_content}}]}
        )
    if tool_call is not None:
        chunks.append({"choices": [{"delta": {"tool_calls": [tool_call]}}]})
    if content is not None:
        chunks.append({"choices": [{"delta": {"content": content}}]})
    chunks.append({"choices": [{"delta": {}, "finish_reason": finish_reason}]})
    body = (
        "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        + "data: [DONE]\n\n"
    )
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


def test_zai_catalog_exposes_documented_thinking_toggle() -> None:
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"))

    try:
        models = {model.id: model for model in provider.list_models()}
    finally:
        provider.close()

    assert "glm-5.1" not in models
    assert models["glm-5.2"].thinking_levels == ("none", "thinking")
    assert models["glm-5.2"].default_thinking_level == "thinking"


def test_zai_register_provider_honors_context_reasoning_effort() -> None:
    provider = register_provider(
        SimpleNamespace(
            env={"ZAI_API_KEY": "test"},
            model="glm-5.2",
            reasoning_effort="none",
        )
    )

    try:
        assert provider.config.reasoning_effort == "none"
    finally:
        provider.close()


def test_zai_provider_sends_thinking_object_for_selected_effort() -> None:
    captured: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _sse_response(content="done")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(
        ZAIConfig(ayoke_key="test", reasoning_effort="thinking"),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])

    assert captured["payload"] == {
        "model": "glm-5.2",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "thinking": {"type": "enabled", "clear_thinking": True},
    }


def test_zai_provider_preserves_structured_tool_history() -> None:
    captured: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _sse_response(content="done")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"), http_client=client)
    assistant = Message(
        role="assistant",
        content="",
        tool_calls=[
            ToolCall(
                id="call_1",
                type="function",
                function=ToolFunction(name="read", arguments='{"path":"README.md"}'),
            )
        ],
    )

    provider.complete(
        [Message.user("read the file"), assistant, Message.tool("call_1", "ok")],
        [],
    )

    assert captured["payload"]["messages"] == [
        {"role": "user", "content": "read the file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"README.md"}'},
                }
            ],
        },
        {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
    ]


def test_zai_provider_can_disable_thinking() -> None:
    captured: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _sse_response(content="done")

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


def test_zai_provider_parses_reasoning_content_from_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            content="done",
            reasoning_content="let me think about it",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"), http_client=client)

    message = provider.complete([Message.user("hello")], [])

    assert message.reasoning_content == "let me think about it"
    provider.close()


def test_zai_provider_does_not_replay_reasoning_content_on_next_request() -> None:
    captured: dict[str, dict[str, object]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return _sse_response(
            content="done",
            reasoning_content="prior reasoning",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"), http_client=client)

    first = provider.complete([Message.user("hello")], [])
    provider.complete([Message.user("hello"), first], [])

    messages = cast(list[dict[str, object]], captured["payload"]["messages"])
    assert "reasoning_content" not in messages[1]
    provider.close()


def test_zai_provider_assembles_streaming_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(
            tool_call={
                "id": "call_1",
                "index": 0,
                "type": "function",
                "function": {"name": "read", "arguments": '{"path":"README.md"}'},
            },
            finish_reason="tool_calls",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"), http_client=client)

    message = provider.complete([Message.user("read the file")], [])
    provider.close()

    assert message.content is None
    assert len(message.tool_calls) == 1
    tool_call = message.tool_calls[0]
    assert tool_call.id == "call_1"
    assert tool_call.function.name == "read"
    assert tool_call.function.arguments == '{"path":"README.md"}'


def test_zai_provider_retries_on_streaming_idle_timeout() -> None:
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ReadTimeout("stream idle timeout simulated")
        return _sse_response(content="done")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(
        ZAIConfig(
            ayoke_key="test",
            read_idle_timeout_seconds=0.2,
            max_retries=3,
            retry_backoff_seconds=0.01,
        ),
        http_client=client,
        sleep=lambda _seconds: None,
    )

    try:
        message = provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    assert attempts["count"] >= 2
    assert message.content == "done"


def test_zai_provider_raises_on_empty_streaming_completion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b"data: [DONE]\n\n",
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = ZAIProvider(ZAIConfig(ayoke_key="test"), http_client=client)

    with pytest.raises(Exception):  # noqa: PT011
        provider.complete([Message.user("hello")], [])
    provider.close()
