# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from typing import cast

import httpx

from yoke.agent.models import Message
from yoke.ai.providers.base import fork_provider
from yoke.ai.providers.opencode_go import OpenCodeGoConfig
from yoke.ai.providers.opencode_go import OpenCodeGoProvider


def test_opencode_go_muse_spark_contributor_catalog_and_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "muse-ok"}],
                    }
                ]
            },
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="muse-spark-1.3-contributor",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        model = provider.current_model_info()
        message = provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert model is not None
    assert model.context_window_tokens == 400_000
    assert model.thinking_levels == ("minimal", "low", "medium", "high", "xhigh")
    assert model.default_thinking_level == "high"
    assert model.supports_image_inputs is True
    assert [item.id for item in provider.list_models()] == [
        "muse-spark-1.3-contributor",
        "glm-5.3-flash",
        "deepseek-v4-flash",
    ]
    assert provider.list_models()[2].context_window_tokens == 400_000
    assert captured["url"] == "https://opencode.ai/zen/go/v1/responses"
    assert payload["model"] == "muse-spark-1.3-contributor"
    assert payload["reasoning"] == {
        "effort": "high",
        "summary": "auto",
        "context": "all_turns",
    }
    assert message.content == "muse-ok"

    minimal_provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="muse-spark-1.3-contributor",
            reasoning_effort="minimal",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        assert minimal_provider.config.reasoning_effort == "minimal"
    finally:
        minimal_provider.close()


def test_opencode_go_responses_honors_zero_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": "retry now"},
            )
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

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="muse-spark-1.3-contributor",
            max_retries=1,
            retry_backoff_seconds=10,
        ),
        http_client=client,
        sleep=delays.append,
    )

    try:
        message = provider.complete([Message.user("hello")], [])
    finally:
        client.close()

    assert message.content == "ok"
    assert calls == 2
    assert delays == [0.0]


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
