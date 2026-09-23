# ruff: noqa: D100,D103,S101

from pathlib import Path
from typing import cast

import pytest

from yoke.agent.models import Message
from yoke.ai.providers.codex.subscription import clamp_reasoning_effort
from yoke.ai.providers.codex.websocket.provider import CodexProvider
from yoke.ai.sdk.providers import build_builtin_provider


@pytest.mark.parametrize("model_id", ["gpt-6-sol", "gpt-6-luna"])
@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_gpt6_sol_luna_selection_and_request_preserve_reasoning(
    tmp_path: Path, model_id: str, effort: str
) -> None:
    provider = build_builtin_provider(
        f"codex:{model_id}:{effort}",
        env={"YOKE_CODEX_API_KEY": "test-key"},
        home=tmp_path,
    )
    assert isinstance(provider, CodexProvider)
    try:
        model = provider.current_model_info()
        assert model is not None
        assert model.id == model_id
        assert model.context_window_tokens == 400_000
        assert model.supports_image_inputs
        assert model.default_thinking_level == "medium"
        assert model.thinking_levels == (
            "none",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
        payload = provider._request_payload([Message.user("hello")], [])
        assert payload["model"] == model_id
        reasoning = payload["reasoning"]
        assert isinstance(reasoning, dict)
        assert cast(dict[str, object], reasoning)["effort"] == effort
    finally:
        provider.close()


@pytest.mark.parametrize("model_id", ["gpt-6-sol", "gpt-6-luna"])
@pytest.mark.parametrize("effort", ["minimal", "invalid"])
def test_gpt6_sol_luna_unsupported_reasoning_defaults_to_medium(
    model_id: str, effort: str
) -> None:
    assert clamp_reasoning_effort(model_id, effort) == "medium"
