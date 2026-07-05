from __future__ import annotations

from pathlib import Path

from yoke.ai.providers.opencode_go import OpenCodeGoProvider
from yoke.ai.providers.resolution import build_provider
from yoke.ai.providers.resolution import is_provider_ready
from yoke.ai.providers.resolution import list_provider_readiness
from yoke.ai.providers.resolution import parse_provider_ref
from yoke.ai.providers.resolution import provider_readiness
from yoke.ai.providers.resolution import provider_status
from yoke.ai.providers.zai import ZAIProvider


def test_parse_provider_ref_accepts_model_and_thinking() -> None:
    provider_ref = parse_provider_ref("ZAI:glm-5.2:none")

    assert provider_ref.provider_name == "zai"
    assert provider_ref.model == "glm-5.2"
    assert provider_ref.reasoning_effort == "none"
    assert provider_ref.qualified_name == "zai:glm-5.2:none"


def test_provider_readiness_uses_explicit_env(tmp_path: Path) -> None:
    missing = provider_status("zai:glm-5.2:none", env={}, home=tmp_path)
    ready = provider_status(
        "zai:glm-5.2:none",
        env={"ZAI_API_KEY": "test"},
        home=tmp_path,
    )

    assert missing.ready is False
    assert missing.reason == "zai provider requires ZAI_API_KEY."
    assert ready.ready is True
    assert ready.model == "glm-5.2"
    assert ready.reasoning_effort == "none"
    assert [model.id for model in ready.models] == ["glm-5.2"]


def test_build_provider_constructs_zai_from_qualified_name(tmp_path: Path) -> None:
    provider = build_provider(
        "zai:glm-5.2:none",
        env={"ZAI_API_KEY": "test"},
        home=tmp_path,
    )

    assert isinstance(provider, ZAIProvider)
    try:
        assert provider.config.model == "glm-5.2"
        assert provider.config.reasoning_effort == "none"
    finally:
        provider.close()


def test_build_provider_constructs_opencode_go_from_explicit_env(
    tmp_path: Path,
) -> None:
    provider = build_provider(
        "opencode-go:kimi-k2.7-code",
        env={"OPENCODE_API_KEY": "test"},
        home=tmp_path,
    )

    assert isinstance(provider, OpenCodeGoProvider)
    try:
        assert provider.config.api_key == "test"
        assert provider.config.model == "kimi-k2.7-code"
    finally:
        provider.close()


def test_provider_readiness_reports_known_providers(tmp_path: Path) -> None:
    readiness = {
        item.provider_name: item for item in provider_readiness(env={}, home=tmp_path)
    }

    assert {"codex", "opencode-go", "zai"} <= set(readiness)
    assert readiness["zai"].ready is False
    assert is_provider_ready("zai", env={}, home=tmp_path) is False


def test_list_provider_readiness_aliases_provider_readiness(tmp_path: Path) -> None:
    assert list_provider_readiness(env={}, home=tmp_path) == provider_readiness(
        env={}, home=tmp_path
    )
