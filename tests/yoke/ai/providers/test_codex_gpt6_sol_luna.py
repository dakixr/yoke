# ruff: noqa: D100,D103,S101

from pathlib import Path
from typing import cast

import pytest

from yoke.agent.models import Message
from yoke.ai.providers.codex.subscription import clamp_reasoning_effort
from yoke.ai.providers.codex.subscription.catalog import list_provider_models
from yoke.ai.providers.codex.websocket.provider import CodexProvider
from yoke.ai.providers.model_selection import UnknownModelError
from yoke.ai.sdk.providers import build_builtin_provider


def test_codex_catalog_only_advertises_gpt6_models() -> None:
    assert [model.id for model in list_provider_models(None)] == [
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6.1-sol",
        "gpt-6-luna",
    ]


def test_codex_rejects_new_gpt5_selection(tmp_path: Path) -> None:
    provider = build_builtin_provider(
        "codex:gpt-6-sol:medium",
        env={"YOKE_CODEX_API_KEY": "test-key"},
        home=tmp_path,
    )
    assert isinstance(provider, CodexProvider)
    try:
        with pytest.raises(UnknownModelError):
            provider.set_model("gpt-5.6-sol")
    finally:
        provider.close()


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


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_gpt61_sol_selection_and_request_preserve_reasoning(
    tmp_path: Path, effort: str
) -> None:
    provider = build_builtin_provider(
        f"codex:gpt-6.1-sol:{effort}",
        env={"YOKE_CODEX_API_KEY": "test-key"},
        home=tmp_path,
    )
    assert isinstance(provider, CodexProvider)
    try:
        model = provider.current_model_info()
        assert model is not None
        assert model.context_window_tokens == 400_000
        assert model.supports_image_inputs
        assert model.default_thinking_level == "medium"
        assert model.thinking_levels == ("low", "medium", "high", "xhigh", "max")
        payload = provider._request_payload([Message.user("hello")], [])
        assert payload["model"] == "gpt-6.1-sol"
        reasoning = payload["reasoning"]
        assert isinstance(reasoning, dict)
        assert cast(dict[str, object], reasoning)["effort"] == effort
    finally:
        provider.close()


@pytest.mark.parametrize("effort", ["none", "minimal", "invalid"])
def test_gpt61_sol_unsupported_reasoning_defaults_to_medium(effort: str) -> None:
    assert clamp_reasoning_effort("gpt-6.1-sol", effort) == "medium"
