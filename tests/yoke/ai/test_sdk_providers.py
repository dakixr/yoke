# ruff: noqa: D100, D103, S101

from __future__ import annotations

import httpx
import pytest
from typing import cast

from yoke.agent.models import Message
from yoke.ai import build_builtin_provider
from yoke.ai.providers import OpenAICompatibleConfig
from yoke.ai.providers import OpenAICompatibleProvider
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.codex.subscription import CodexSubscriptionProvider
from yoke.ai.providers.opencode_go import OpenCodeGoProvider
from yoke.ai.providers.zai import ZAIProvider


def test_openai_compatible_provider_does_not_retry_read_timeout() -> None:
    calls = 0

    def timeout(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow response")

    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(api_key="test", model="test", max_retries=8),
        http_client=httpx.Client(transport=httpx.MockTransport(timeout)),
        sleep=lambda _seconds: None,
    )

    with pytest.raises(ProviderError, match="timed out"):
        provider.complete([Message.user("hello")], [])

    assert calls == 1


def test_build_builtin_provider_accepts_selection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOKE_CODEX_API_KEY", "test-key")

    provider = cast(
        CodexSubscriptionProvider,
        build_builtin_provider("codex:gpt-5.6-sol:high"),
    )

    config = provider.config
    assert config.model == "gpt-5.6-sol"
    assert config.reasoning_effort == "high"
    provider.close()


def test_build_builtin_provider_rejects_unknown_provider_selection() -> None:
    with pytest.raises(ValueError, match="Unsupported provider 'legacy'"):
        build_builtin_provider("legacy:gpt-test:high")


def test_build_builtin_provider_rejects_unknown_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "test-key")

    with pytest.raises(ValueError, match="Unknown model 'glm-missing'"):
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


def test_build_builtin_opencode_luna_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_API_KEY", "test-key")

    provider = cast(
        OpenCodeGoProvider,
        build_builtin_provider("opencode-go:gpt-5.6-luna:max"),
    )

    config = provider.config
    assert config.model == "gpt-5.6-luna"
    assert config.reasoning_effort == "max"
    provider.close()
