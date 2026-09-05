"""Canonical lazy registry for Yoke's built-in providers."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Mapping
from importlib import import_module
from pathlib import Path
from typing import cast

from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.plugins import ProviderPluginContext

BUILTIN_PROVIDER_NAMES = ("codex", "opencode-go", "zai")
_PROVIDER_MODULES = {
    "codex": "yoke.ai.providers.codex.websockets",
    "opencode-go": "yoke.ai.providers.opencode_go",
    "zai": "yoke.ai.providers.zai",
}

type ProviderFactory = Callable[[ProviderPluginContext], Provider]
type ModelLister = Callable[[ProviderPluginContext], list[ProviderModelInfo]]


def build_registered_provider(
    provider_name: str,
    *,
    model: str | None,
    reasoning_effort: str | None,
    session_id: str | None,
    env: Mapping[str, str],
    home: Path,
) -> Provider:
    """Construct one built-in provider from its lazily imported factory."""
    factory = cast(
        ProviderFactory | None,
        _load_target(provider_name, "register_provider"),
    )
    if factory is None:
        raise ValueError(f"Unsupported built-in provider {provider_name!r}.")
    return factory(
        _provider_context(
            provider_name,
            model=model,
            reasoning_effort=reasoning_effort,
            session_id=session_id,
            env=env,
            home=home,
        )
    )


def list_registered_models(
    provider_name: str,
    *,
    model: str | None,
    reasoning_effort: str | None,
    env: Mapping[str, str],
    home: Path,
) -> list[ProviderModelInfo] | None:
    """Return copied model metadata for one built-in provider."""
    lister = cast(
        ModelLister | None,
        _load_target(provider_name, "list_provider_models"),
    )
    if lister is None:
        return None
    return [
        item.model_copy(deep=True)
        for item in lister(
            _provider_context(
                provider_name,
                model=model,
                reasoning_effort=reasoning_effort,
                env=env,
                home=home,
            )
        )
    ]


def _provider_context(
    provider_name: str,
    *,
    model: str | None,
    reasoning_effort: str | None,
    session_id: str | None = None,
    env: Mapping[str, str],
    home: Path,
) -> ProviderPluginContext:
    return ProviderPluginContext(
        name=provider_name,
        home=home.resolve(),
        model=model,
        reasoning_effort=reasoning_effort,
        session_id=session_id,
        env=env,
    )


def _load_target(provider_name: str, attribute: str) -> object | None:
    module_name = _PROVIDER_MODULES.get(provider_name)
    if module_name is None:
        return None
    return getattr(import_module(module_name), attribute)
