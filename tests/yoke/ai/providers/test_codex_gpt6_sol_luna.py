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


def test_codex_catalog_advertises_current_models() -> None:
    assert [model.id for model in list_provider_models(None)] == [
        "gpt-6.2-sol",
        "gpt-6.1-sol",
        "gpt-6.1-terra",
        "gpt-6-luna",
    ]


@pytest.mark.parametrize("retired", ["gpt-5.6-sol", "gpt-6-sol", "gpt-6-astra"])
def test_codex_rejects_retired_selection(tmp_path: Path, retired: str) -> None:
    provider = build_builtin_provider(
        "codex:gpt-6.1-sol:medium",
        env={"YOKE_CODEX_API_KEY": "test-key"},
        home=tmp_path,
    )
    assert isinstance(provider, CodexProvider)
    try:
        with pytest.raises(UnknownModelError):
            provider.set_model(retired)
    finally:
        provider.close()


MODELS = {
    "gpt-6.2-sol": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-terra": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
}


@pytest.mark.parametrize("model_id", MODELS)
def test_codex_models_advertise_current_metadata(tmp_path: Path, model_id: str) -> None:
    provider = build_builtin_provider(
        f"codex:{model_id}:medium",
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
        assert model.thinking_levels == MODELS[model_id]
    finally:
        provider.close()


@pytest.mark.parametrize("model_id", MODELS)
def test_codex_request_preserves_selected_effort(tmp_path: Path, model_id: str) -> None:
    provider = build_builtin_provider(
        f"codex:{model_id}:high",
        env={"YOKE_CODEX_API_KEY": "test-key"},
        home=tmp_path,
    )
    assert isinstance(provider, CodexProvider)
    try:
        payload = provider._request_payload([Message.user("hello")], [])
        assert payload["model"] == model_id
        reasoning = payload["reasoning"]
        assert isinstance(reasoning, dict)
        assert cast(dict[str, object], reasoning)["effort"] == "high"
    finally:
        provider.close()


@pytest.mark.parametrize("model_id", MODELS)
@pytest.mark.parametrize("effort", ["none", "minimal", "invalid"])
def test_codex_unsupported_reasoning_defaults_to_medium(
    model_id: str, effort: str
) -> None:
    expected = "medium"
    if effort == "none" and "none" in MODELS[model_id]:
        expected = "none"
    assert clamp_reasoning_effort(model_id, effort) == expected
