from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

import pytest

from yoke.ai.providers.opencode_go import OpenCodeGoProvider
from yoke.ai.providers.credentials import save_provider_credential
from yoke.ai.providers.plugins import create_custom_provider
from yoke.ai.providers.plugins import LoadedProviderPlugin
from yoke.ai.providers.resolution import build_provider
from yoke.ai.providers.resolution import parse_provider_ref
from yoke.ai.providers.resolution import provider_status
from yoke.ai.providers.zai import ZAIProvider


def test_parse_provider_ref_accepts_model_and_thinking() -> None:
    provider_ref = parse_provider_ref("ZAI:glm-5.3-flash:max")

    assert provider_ref.provider_name == "zai"
    assert provider_ref.model == "glm-5.3-flash"
    assert provider_ref.reasoning_effort == "max"
    assert provider_ref.qualified_name == "zai:glm-5.3-flash:max"


def test_provider_readiness_uses_explicit_env(tmp_path: Path) -> None:
    missing = provider_status("zai:glm-5.3-flash:max", env={}, home=tmp_path)
    ready = provider_status(
        "zai:glm-5.3-flash:max",
        env={"ZAI_API_KEY": "test"},
        home=tmp_path,
    )

    assert missing.ready is False
    assert missing.reason == "zai provider requires ZAI_API_KEY."
    assert ready.ready is True
    assert ready.model == "glm-5.3-flash"
    assert ready.reasoning_effort == "max"
    assert [model.id for model in ready.models] == ["glm-5.3-flash"]


def test_provider_readiness_uses_credentials_saved_by_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    save_provider_credential(home=tmp_path, name="ZAI_API_KEY", value="saved")

    ready = provider_status("zai:glm-5.3-flash:max", home=tmp_path)

    assert ready.ready is True


def test_codex_readiness_recognizes_account_vault_credentials(
    tmp_path: Path,
) -> None:
    account_auth = tmp_path / ".codex-auth" / "accounts" / "work" / "auth.json"
    account_auth.parent.mkdir(parents=True)
    account_auth.write_text("{}", encoding="utf-8")

    ready = provider_status("codex:gpt-5.6-sol", env={}, home=tmp_path)

    assert ready.ready is True


def test_build_provider_constructs_zai_from_qualified_name(tmp_path: Path) -> None:
    provider = build_provider(
        "zai:glm-5.3-flash:max",
        env={"ZAI_API_KEY": "test"},
        home=tmp_path,
    )

    assert isinstance(provider, ZAIProvider)
    try:
        assert provider.config.model == "glm-5.3-flash"
        assert provider.config.reasoning_effort == "max"
    finally:
        provider.close()


@pytest.mark.parametrize(
    ("selection", "expected_effort"),
    [("zai", "max"), ("zai:glm-5.3-flash:medium", "max")],
)
def test_build_provider_uses_zai_model_default_for_alias_effort(
    tmp_path: Path,
    selection: str,
    expected_effort: str,
) -> None:
    provider = build_provider(
        selection,
        env={"ZAI_API_KEY": "test"},
        home=tmp_path,
    )

    assert isinstance(provider, ZAIProvider)
    try:
        assert provider.config.model == "glm-5.3-flash"
        assert provider.config.reasoning_effort == expected_effort
    finally:
        provider.close()


def test_build_provider_constructs_opencode_go_from_explicit_env(
    tmp_path: Path,
) -> None:
    provider = build_provider(
        "opencode-go:glm-5.3-flash",
        env={"OPENCODE_API_KEY": "test"},
        home=tmp_path,
        session_id="saved-session",
    )

    assert isinstance(provider, OpenCodeGoProvider)
    try:
        assert provider.config.api_key == "test"
        assert provider.config.model == "glm-5.3-flash"
        assert provider.config.reasoning_effort == "max"
        assert provider.config.session_id == "saved-session"
    finally:
        provider.close()


def test_custom_provider_catalog_normalizes_constructed_default(
    tmp_path: Path,
) -> None:
    provider_dir = tmp_path / ".yoke" / "providers"
    provider_dir.mkdir(parents=True)
    (provider_dir / "demo.py").write_text(
        """
from types import SimpleNamespace

from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderModelInfo

PROVIDER_NAME = "demo"


class DemoProvider:
    provider_name = PROVIDER_NAME

    def __init__(self):
        self.config = SimpleNamespace(model="demo-model", reasoning_effort="medium")

    def complete(self, messages, tools):
        return Message.assistant("done")


def register_provider(context):
    return DemoProvider()


def list_provider_models(context):
    if context.reasoning_effort is not None:
        raise ValueError("catalog discovery must not inherit thinking effort")
    return [
        ProviderModelInfo(
            id="demo-model",
            display_name="Demo Model",
            context_window_tokens=1000,
            thinking_levels=("none", "thinking"),
            default_thinking_level="thinking",
        )
    ]
""".strip(),
        encoding="utf-8",
    )

    provider = build_provider("demo", env={}, home=tmp_path)

    custom_provider = cast(Any, provider)
    assert custom_provider.config.model == "demo-model"
    assert custom_provider.config.reasoning_effort == "thinking"


def test_custom_provider_closes_invalid_factory_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class InvalidProvider:
        def close(self) -> None:
            closed.append(True)

    plugin = LoadedProviderPlugin(
        name="demo",
        source_path=tmp_path / "demo.py",
        factory=lambda _context: cast(Any, InvalidProvider()),
    )
    monkeypatch.setattr(
        "yoke.ai.providers.plugins.load_global_provider_plugins",
        lambda **_kwargs: [plugin],
    )

    with pytest.raises(ValueError, match="is invalid"):
        create_custom_provider("demo", home=tmp_path, env={})

    assert closed == [True]


def test_custom_provider_cleanup_failure_does_not_mask_catalog_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class ProviderWithBrokenClose:
        def complete(self, messages: object, tools: object) -> None:
            del messages, tools

        def close(self) -> None:
            closed.append(True)
            raise RuntimeError("cleanup failed")

    plugin = LoadedProviderPlugin(
        name="demo",
        source_path=tmp_path / "demo.py",
        factory=lambda _context: cast(Any, ProviderWithBrokenClose()),
        list_models=lambda _context: (_ for _ in ()).throw(
            RuntimeError("catalog failed")
        ),
    )
    monkeypatch.setattr(
        "yoke.ai.providers.plugins.load_global_provider_plugins",
        lambda **_kwargs: [plugin],
    )

    with pytest.raises(RuntimeError, match="catalog failed"):
        create_custom_provider("demo", home=tmp_path, env={})

    assert closed == [True]


def test_custom_provider_does_not_close_when_factory_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[bool] = []

    class FailingFactory:
        def __call__(self, _context: object) -> Any:
            raise RuntimeError("factory failed")

        def close(self) -> None:
            closed.append(True)

    plugin = LoadedProviderPlugin(
        name="demo",
        source_path=tmp_path / "demo.py",
        factory=FailingFactory(),
    )
    monkeypatch.setattr(
        "yoke.ai.providers.plugins.load_global_provider_plugins",
        lambda **_kwargs: [plugin],
    )

    with pytest.raises(ValueError, match="factory failed"):
        create_custom_provider("demo", home=tmp_path, env={})

    assert closed == []


def test_provider_status_reports_model_catalog_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_to_list_models(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(
        "yoke.ai.providers.resolution.list_provider_models",
        fail_to_list_models,
    )

    status = provider_status(
        "zai:glm-5.3-flash", env={"ZAI_API_KEY": "test"}, home=tmp_path
    )

    assert status.ready is False
    assert status.reason == (
        "Could not list models for provider `zai`: catalog unavailable"
    )


def test_provider_status_reports_close_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class ProviderWithBrokenClose:
        config = SimpleNamespace(model="glm-5.3-flash", reasoning_effort="high")

        def complete(self, messages: object, tools: object) -> None:
            del messages, tools

        def close(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(
        "yoke.ai.providers.resolution.build_provider",
        lambda *args, **kwargs: ProviderWithBrokenClose(),
    )

    status = provider_status(
        "zai:glm-5.3-flash", env={"ZAI_API_KEY": "test"}, home=tmp_path
    )

    assert status.ready is False
    assert status.reason == "Could not close provider `zai`: close failed"
    assert status.model == "glm-5.3-flash"
    assert status.reasoning_effort == "high"
