# ruff: noqa: D100,D103,S101

from pathlib import Path
from typing import cast

import pytest

from yoke.agent.models import Message
from yoke.ai.providers.codex.subscription import clamp_reasoning_effort
from yoke.ai.providers.codex.websocket.provider import CodexProvider
from yoke.ai.sdk.providers import build_builtin_provider


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_astra_selection_and_request_preserve_reasoning(
    tmp_path: Path, effort: str
) -> None:
    provider = build_builtin_provider(
        f"codex:gpt-6-astra:{effort}",
        env={"YOKE_CODEX_API_KEY": "test-key"},
        home=tmp_path,
    )
    assert isinstance(provider, CodexProvider)
    try:
        model = provider.current_model_info()
        assert model is not None
        assert model.id == "gpt-6-astra"
        assert model.context_window_tokens == 400_000
        assert model.supports_image_inputs
        assert model.default_thinking_level == "medium"
        assert model.thinking_levels == ("low", "medium", "high", "xhigh", "max")
        payload = provider._request_payload([Message.user("hello")], [])
        assert payload["model"] == "gpt-6-astra"
        reasoning = payload["reasoning"]
        assert isinstance(reasoning, dict)
        assert cast(dict[str, object], reasoning)["effort"] == effort
    finally:
        provider.close()


@pytest.mark.parametrize("effort", ["none", "minimal", "invalid"])
def test_astra_unsupported_reasoning_defaults_to_medium(effort: str) -> None:
    assert clamp_reasoning_effort("gpt-6-astra", effort) == "medium"
