"""Go streaming protocol regressions without changing shared request projection."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderCancelledError, ProviderError
from yoke.ai.providers.opencode_go import OpenCodeGoConfig, OpenCodeGoProvider
from yoke.ai.providers.openai_compat import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)


def event(value: object) -> bytes:
    return ("data: " + json.dumps(value) + "\n\n").encode()


def delta(value: dict, finish: str | None = None) -> bytes:
    return event({"choices": [{"index": 0, "delta": value, "finish_reason": finish}]})


def provider(handler, **kwargs) -> OpenCodeGoProvider:
    return OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", max_retries=0, **kwargs),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_fragmented_interleaved_tools_reasoning_and_usage() -> None:
    body = b": heartbeat\n\n" + b"".join(
        [
            delta({"reasoning_content": "check "}),
            delta({"content": "Hello ", "reasoning_content": "files"}),
            delta(
                {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "b",
                            "function": {"name": "read", "arguments": '{"path":'},
                        },
                        {
                            "index": 0,
                            "id": "a",
                            "function": {"name": "rg", "arguments": '{"q":'},
                        },
                    ]
                }
            ),
            delta(
                {
                    "content": "world",
                    "tool_calls": [
                        {"index": 0, "function": {"arguments": '"x"}'}},
                        {"index": 1, "function": {"arguments": '"a.py"}'}},
                    ],
                },
                "tool_calls",
            ),
            event(
                {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "total_tokens": 120,
                        "prompt_tokens_details": {"cached_tokens": 80},
                        "completion_tokens_details": {"reasoning_tokens": 10},
                    },
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )

    class Fragments(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            for i in range(0, len(body), 7):
                yield body[i : i + 7]

    p = provider(lambda _: httpx.Response(200, stream=Fragments()))
    try:
        message = p.complete([Message.user("hello")], [])
        assert message.content == "Hello world"
        assert message.reasoning_content == "check files"
        assert [c.id for c in message.tool_calls] == ["a", "b"]
        assert [c.function.arguments for c in message.tool_calls] == [
            '{"q":"x"}',
            '{"path":"a.py"}',
        ]
        assert message.usage is not None
        assert message.usage.cached_input_tokens == 80
        assert message.usage.reasoning_tokens == 10
        assert message.usage.total_tokens == 120
        assert message.usage.model_id == "glm-5.3-flash"
    finally:
        p.close()


@pytest.mark.parametrize(
    "body",
    [
        delta({"content": "partial"}),
        delta({"content": "partial"}, "stop"),
        b"data: [DONE]\n\n",
        b"data: not-json\n\n",
        event({"error": {"message": "upstream failure"}}),
        delta(
            {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
            "tool_calls",
        )
        + b"data: [DONE]\n\n",
    ],
)
def test_bad_stream_never_returns_partial_answer_or_retries(body: bytes) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=body)

    p = provider(handler)
    try:
        with pytest.raises(ProviderError):
            p.complete([Message.user("hello")], [])
        assert len(calls) == 1
    finally:
        p.close()


def test_cancel_during_stream_closes_response() -> None:
    cancelled = False
    closed = False

    class Stream(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            nonlocal cancelled
            yield delta({"content": "partial"})
            cancelled = True
            yield delta({}, "stop")

        def close(self) -> None:
            nonlocal closed
            closed = True

    p = provider(lambda _: httpx.Response(200, stream=Stream()))
    try:
        with pytest.raises(ProviderCancelledError):
            p.complete_with_cancel(
                [Message.user("hello")], [], cancel_requested=lambda: cancelled
            )
        assert closed
    finally:
        p.close()


def test_rate_limit_retries_before_stream_with_same_session() -> None:
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(
                429, headers={"Retry-After": "0"}, json={"error": "busy"}
            )
        return httpx.Response(
            200, content=delta({"content": "ok"}, "stop") + b"data: [DONE]\n\n"
        )

    p = provider(handler)
    p._openai_provider.config.max_retries = 1
    try:
        assert p.complete([Message.user("hello")], []).content == "ok"
        assert len(calls) == 2
        assert calls[0].content == calls[1].content
        assert (
            calls[0].headers["x-opencode-session"]
            == calls[1].headers["x-opencode-session"]
        )
    finally:
        p.close()


@pytest.mark.parametrize(
    "messages",
    [
        [Message.system("base instructions"), Message.user("hello")],
        [
            Message.user("read"),
            Message.model_validate(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "read",
                                "arguments": '{"path":"a.py"}',
                            },
                        }
                    ],
                    "reasoning_content": "inspect file",
                }
            ),
            Message.model_validate(
                {"role": "tool", "tool_call_id": "call-1", "content": "file contents"}
            ),
        ],
        [
            Message.model_validate(
                {"role": "tool", "tool_call_id": "orphan", "content": "retained result"}
            ),
            Message.user("continue"),
        ],
        [
            Message.system("base"),
            Message.user("first"),
            Message.assistant("answer"),
            Message.system("Active skill: review"),
            Message.user("next"),
        ],
        [
            Message.system("base"),
            Message.user("Branch summary: previous work"),
            Message.assistant("compaction checkpoint"),
            Message.user("continue"),
        ],
        [
            Message.model_validate(
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "inspect"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                }
            )
        ],
    ],
)
def test_request_projection_matches_established_converter(
    messages: list[Message],
) -> None:
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if body.get("stream"):
            return httpx.Response(
                200, content=delta({"content": "ok"}, "stop") + b"data: [DONE]\n\n"
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    old = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key="test",
            model="glm-5.3-flash",
            max_tokens=65536,
            reasoning_effort="max",
        ),
        http_client=client,
    )
    new = provider(handler)
    tools: list[dict[str, object]] = [
        {
            "type": "function",
            "function": {"name": "read", "parameters": {"type": "object"}},
        }
    ]
    try:
        old.complete(messages, tools)
        new.complete(messages, tools)
        before, after = bodies
        before.pop("reasoning_effort")
        assert after.pop("stream") is True
        assert after.pop("stream_options") == {"include_usage": True}
        assert before == after
    finally:
        old.close()
        new.close()
