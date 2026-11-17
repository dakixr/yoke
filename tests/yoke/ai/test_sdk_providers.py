# ruff: noqa: D100, D103, S101

from __future__ import annotations

import httpx
import pytest

from yoke.ai.providers.model_selection import UnknownModelError
from typing import cast

from yoke.agent.models import Message
from yoke.ai import build_builtin_provider
from yoke.ai.providers import OpenAICompatibleConfig
from yoke.ai.providers import OpenAICompatibleProvider
from yoke.ai.providers.openai_compat.client import LazyHttpClient
from yoke.ai.providers.zai import ZAIProvider


def test_openai_compatible_provider_rebuilds_owned_client_after_read_error() -> None:
    clients: list[httpx.Client] = []

    def make_client() -> httpx.Client:
        generation = len(clients)

        def handler(request: httpx.Request) -> httpx.Response:
            if generation == 0:
                raise httpx.ReadError(
                    "[WinError 10054] connection reset",
                    request=request,
                )
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": "recovered"}}
                    ]
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return client

    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key="test",
            model="test",
            max_retries=1,
            retry_backoff_seconds=0,
            max_retry_backoff_seconds=0,
        ),
        sleep=lambda _seconds: None,
    )
    provider._client = LazyHttpClient(None, make_client)

    try:
        message = provider.complete([Message.user("hello")], [])
        assert message.content == "recovered"
        assert len(clients) == 2
        assert clients[0].is_closed
        assert not clients[1].is_closed
    finally:
        provider.close()

    assert clients[1].is_closed


def test_build_builtin_provider_rejects_unknown_provider_selection() -> None:
    with pytest.raises(ValueError, match="Unsupported provider 'legacy'"):
        build_builtin_provider("legacy:gpt-test:high")


def test_build_builtin_provider_rejects_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "test-key")

    with pytest.raises(UnknownModelError, match="Unknown model 'glm-missing'"):
        build_builtin_provider("zai:glm-missing:thinking")


def test_build_builtin_provider_uses_default_for_invalid_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "test-key")

    provider = cast(
        ZAIProvider,
        build_builtin_provider("zai:glm-5.3-flash:thinking"),
    )

    try:
        assert provider.config.model == "glm-5.3-flash"
        assert provider.config.reasoning_effort == "max"
    finally:
        provider.close()
