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
_PROVIDER_TARGETS = {
    "codex": ("yoke.ai.providers.codex.websockets", "register_provider"),
    "opencode-go": ("yoke.ai.providers.opencode_go", "register_provider"),
    "zai": ("yoke.ai.providers.zai", "register_provider"),
}
_MODEL_LISTER_TARGETS = {
    "codex": ("yoke.ai.providers.codex.websockets", "list_provider_models"),
    "opencode-go": ("yoke.ai.providers.opencode_go", "list_provider_models"),
    "zai": ("yoke.ai.providers.zai", "list_provider_models"),
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
        _load_target(_PROVIDER_TARGETS, provider_name),
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
        _load_target(_MODEL_LISTER_TARGETS, provider_name),
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


def _load_target(
    targets: Mapping[str, tuple[str, str]],
    provider_name: str,
) -> object | None:
    target = targets.get(provider_name)
    if target is None:
        return None
    module_name, attribute = target
    return getattr(import_module(module_name), attribute)
