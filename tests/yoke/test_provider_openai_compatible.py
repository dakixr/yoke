# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
import httpx
from typing import cast

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.openai_compat import OpenAICompatibleConfig
from yoke.ai.providers.openai_compat import OpenAICompatibleProvider

TEST_MODEL_CATALOG = (
    ProviderModelInfo(
        id="gpt-a",
        display_name="GPT A",
        context_window_tokens=128_000,
        thinking_levels=("none", "low", "medium", "high", "xhigh"),
        supports_image_inputs=True,
    ),
    ProviderModelInfo(
        id="gpt-b",
        display_name="GPT B",
        context_window_tokens=128_000,
        thinking_levels=("none", "low", "medium", "high", "xhigh"),
        supports_image_inputs=True,
    ),
)


def test_openai_compatible_config_from_env(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "standard-key")
    monkeypatch.setenv("OPENAI_MODEL", "standard-model")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://standard.example/v1")

    config = OpenAICompatibleConfig.from_env()

    assert config.api_key == "standard-key"
    assert config.model == "standard-model"
    assert config.base_url == "https://standard.example/v1"


def test_openai_compatible_provider_preserves_message_phase() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I will inspect the config first.",
                            "phase": "commentary",
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            base_url="https://example.openai.com/v1",
        ),
        http_client=client,
    )

    message = provider.complete([Message.user("hello")], [])

    assert message.content == "I will inspect the config first."
    assert message.phase == "commentary"


def test_openai_compatible_provider_includes_reasoning_effort() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            base_url="https://example.openai.com/v1",
            reasoning_effort="medium",
        ),
        http_client=client,
    )

    provider.complete([Message.user("hello")], [])

    assert captured["payload"] == {
        "model": "gpt-test",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": "medium",
    }


def test_openai_compatible_provider_preserves_reasoning_content() -> None:
    captured_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "done",
                            "reasoning_content": "hidden chain",
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="deepseek-v4-pro",
            base_url="https://example.com/v1",
            reasoning_effort="medium",
        ),
        http_client=client,
    )

    message = provider.complete([Message.user("hello")], [])

    assert message.reasoning_content == "hidden chain"

    provider.complete(
        [Message.user("hello"), message, Message.user("next")],
        [],
    )

    second_payload_messages = cast(
        list[dict[str, object]], captured_payloads[1]["messages"]
    )
    assert second_payload_messages[1]["reasoning_content"] == "hidden chain"


def test_openai_compatible_provider_preserves_usage() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 30,
                    "total_tokens": 130,
                    "completion_tokens_details": {"reasoning_tokens": 25},
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            base_url="https://example.openai.com/v1",
        ),
        http_client=client,
    )

    message = provider.complete([Message.user("hello")], [])

    assert message.usage is not None
    assert message.usage.input_tokens == 100
    assert message.usage.output_tokens == 30
    assert message.usage.reasoning_tokens == 25


def test_openai_compatible_provider_handles_null_tool_call_content() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            base_url="https://example.openai.com/v1",
        ),
        http_client=client,
    )

    provider.complete(
        [
            Message.user("hello"),
            Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call_123",
                        function=ToolFunction(
                            name="rg",
                            arguments='{"raw_args":"-n hello ."}',
                        ),
                    )
                ],
            ),
            Message.tool("call_123", '{"ok": true}'),
        ],
        [],
    )

    assert captured["payload"] == {
        "model": "gpt-test",
        "messages": [
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_123",
                        "type": "function",
                        "function": {
                            "name": "rg",
                            "arguments": '{"raw_args":"-n hello ."}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": '{"ok": true}',
                "tool_call_id": "call_123",
            },
        ],
    }


def test_openai_compatible_provider_retries_transient_disconnect() -> None:
    attempts = 0
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.RemoteProtocolError(
                "Server disconnected without sending a response."
            )
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            base_url="https://example.openai.com/v1",
            max_retries=2,
            retry_backoff_seconds=0.01,
            max_retry_backoff_seconds=0.02,
        ),
        http_client=client,
        sleep=slept.append,
    )

    message = provider.complete([Message.user("hello")], [])

    assert attempts == 2
    assert len(slept) == 1
    assert message.content == "done"


def test_openai_compatible_config_rejects_invalid_reasoning_effort() -> None:
    import pytest

    with pytest.raises(ValueError, match="reasoning_effort"):
        OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            reasoning_effort="turbo",
        )


def test_openai_compatible_provider_exposes_model_catalog() -> None:
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "done",
                            }
                        }
                    ]
                },
            )
        )
    )
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            provider_name="demo",
            model_catalog=(
                TEST_MODEL_CATALOG[0].model_copy(
                    update={"id": "gpt-test", "display_name": "GPT Test"}
                ),
            ),
        ),
        http_client=client,
    )
    current_model = provider.current_model_info()

    assert provider.provider_name == "demo"
    assert provider.current_model_id() == "gpt-test"
    assert current_model is not None
    assert current_model.id == "gpt-test"
    assert provider.list_models()[0].display_name == "GPT Test"


def test_openai_compatible_provider_set_model_validates_reasoning_effort() -> None:
    import pytest

    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "done",
                            }
                        }
                    ]
                },
            )
        )
    )
    provider = OpenAICompatibleProvider(
        config=OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-a",
            provider_name="demo",
            model_catalog=(
                TEST_MODEL_CATALOG[0].model_copy(
                    update={"id": "gpt-a", "display_name": "GPT A"}
                ),
                TEST_MODEL_CATALOG[1].model_copy(
                    update={"id": "gpt-b", "display_name": "GPT B"}
                ),
            ),
        ),
        http_client=client,
    )

    provider.set_model("gpt-b", reasoning_effort="high")

    assert provider.config.model == "gpt-b"
    assert provider.config.reasoning_effort == "high"

    with pytest.raises(ValueError, match="Unknown model"):
        provider.set_model("missing")
