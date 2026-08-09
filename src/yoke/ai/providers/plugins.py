"""Custom provider plugin discovery and construction."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from typing import cast

from yoke.ai.providers.base import ModelCatalogProvider
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import set_config_model_from_catalog


@dataclass(slots=True, frozen=True)
class ProviderPluginContext:
    """ProviderPluginContext."""

    name: str
    home: Path
    env: Mapping[str, str]
    model: str | None = None
    reasoning_effort: str | None = None
    session_id: str | None = None


@dataclass(slots=True, frozen=True)
class LoadedProviderPlugin:
    """LoadedProviderPlugin."""

    name: str
    source_path: Path
    factory: Callable[[ProviderPluginContext], Provider]
    list_models: Callable[[ProviderPluginContext], list[ProviderModelInfo]] | None = (
        None
    )


@dataclass(slots=True, frozen=True)
class ProviderPluginLoadFailure:
    """A provider plugin that could not be imported or registered."""

    source_path: Path
    error: str


@dataclass(slots=True, frozen=True)
class ProviderPluginDiscovery:
    """Valid provider plugins and failures found during one discovery pass."""

    plugins: tuple[LoadedProviderPlugin, ...]
    failures: tuple[ProviderPluginLoadFailure, ...]


RegisterProviderFunc = Callable[[ProviderPluginContext], Provider]
ListProviderModelsFunc = Callable[[ProviderPluginContext], list[ProviderModelInfo]]


def load_global_provider_plugins(*, home: Path) -> list[LoadedProviderPlugin]:
    """Load valid global provider plugins, omitting invalid plugins."""
    return list(discover_global_provider_plugins(home=home).plugins)


def discover_global_provider_plugins(*, home: Path) -> ProviderPluginDiscovery:
    """Discover global provider plugins without discarding load failures."""
    resolved_home = home.resolve()
    provider_dir = resolved_home / ".yoke" / "providers"
    if not provider_dir.is_dir():
        return ProviderPluginDiscovery(plugins=(), failures=())
    plugins: list[LoadedProviderPlugin] = []
    failures: list[ProviderPluginLoadFailure] = []
    seen: dict[str, LoadedProviderPlugin] = {}
    for path in _iter_provider_module_paths(provider_dir):
        try:
            module = _load_provider_module(path)
            register_provider = getattr(module, "register_provider", None)
            list_provider_models = getattr(module, "list_provider_models", None)
            if not callable(register_provider):
                raise ValueError(
                    f"Provider plugin `{path}` is invalid. Define "
                    "`register_provider(context)` to return a "
                    "provider instance."
                )
            if list_provider_models is not None and not callable(list_provider_models):
                raise ValueError(
                    f"Provider plugin `{path}` is invalid. "
                    "`list_provider_models(context)` must be callable "
                    "when defined."
                )
            provider_name = getattr(module, "PROVIDER_NAME", path.stem)
            if not isinstance(provider_name, str) or not provider_name.strip():
                raise ValueError(
                    f"Provider plugin `{path}` is invalid. "
                    "`PROVIDER_NAME` must be a non-empty string."
                )
            normalized_name = provider_name.strip().lower()
            existing = seen.get(normalized_name)
            if existing is not None:
                raise ValueError(
                    f"Conflicting provider name `{normalized_name}` from "
                    f"`{path}`. It is already registered by "
                    f"`{existing.source_path}`."
                )
        except Exception as exc:
            sys.modules.pop(_provider_module_name(path), None)
            failures.append(ProviderPluginLoadFailure(source_path=path, error=str(exc)))
            continue
        plugin = LoadedProviderPlugin(
            name=normalized_name,
            source_path=path,
            factory=cast(RegisterProviderFunc, register_provider),
            list_models=(
                cast(ListProviderModelsFunc, list_provider_models)
                if callable(list_provider_models)
                else None
            ),
        )
        plugins.append(plugin)
        seen[plugin.name] = plugin
    return ProviderPluginDiscovery(
        plugins=tuple(plugins),
        failures=tuple(failures),
    )


def create_custom_provider(
    name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    session_id: str | None = None,
    home: Path,
    env: Mapping[str, str] | None = None,
) -> Provider | None:
    """create_custom_provider."""
    normalized = name.strip().lower()
    resolved_env = os.environ if env is None else env
    for plugin in load_global_provider_plugins(home=home):
        if plugin.name != normalized:
            continue
        context = ProviderPluginContext(
            name=plugin.name,
            home=home.resolve(),
            model=model,
            reasoning_effort=reasoning_effort if model is not None else None,
            session_id=session_id,
            env=resolved_env,
        )
        try:
            provider = plugin.factory(context)
        except Exception as exc:
            raise ValueError(
                f"Could not initialize provider `{plugin.name}` "
                f"from `{plugin.source_path}`: {exc}"
            ) from exc
        if not callable(getattr(provider, "complete", None)):
            raise ValueError(
                f"Provider `{plugin.name}` from `{plugin.source_path}` "
                "is invalid. It must return an object with "
                "`complete(messages, tools)`."
            )
        _attach_plugin_model_catalog(provider, plugin=plugin, context=context)
        return provider
    return None


def available_custom_provider_names(*, home: Path) -> list[str]:
    """available_custom_provider_names."""
    return [plugin.name for plugin in load_global_provider_plugins(home=home)]


def list_custom_provider_models(
    name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    home: Path,
    env: Mapping[str, str] | None = None,
) -> list[ProviderModelInfo] | None:
    """Return a custom provider plugin model catalog when available."""
    normalized = name.strip().lower()
    resolved_env = os.environ if env is None else env
    for plugin in load_global_provider_plugins(home=home):
        if plugin.name != normalized:
            continue
        context = ProviderPluginContext(
            name=plugin.name,
            home=home.resolve(),
            model=model,
            reasoning_effort=reasoning_effort,
            env=resolved_env,
        )
        if plugin.list_models is not None:
            try:
                return [
                    model_info.model_copy(deep=True)
                    for model_info in plugin.list_models(context)
                ]
            except Exception as exc:
                raise ValueError(
                    "Could not load model catalog for provider "
                    f"`{plugin.name}` "
                    f"from `{plugin.source_path}`: {exc}"
                ) from exc
        provider = plugin.factory(context)
        try:
            if isinstance(provider, ModelCatalogProvider):
                return provider.list_models()
            return None
        finally:
            close = getattr(provider, "close", None)
            if callable(close):
                close()
    return None


def _iter_provider_module_paths(directory: Path) -> list[Path]:
    return [
        path
        for path in sorted(directory.glob("*.py"))
        if path.name != "__init__.py" and not path.name.startswith("_")
    ]


def _load_provider_module(path: Path) -> ModuleType:
    module_name = _provider_module_name(path)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ValueError(f"Unable to load provider module: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise ValueError(
            f"Could not load provider plugin `{path}`. "
            f"The Python module failed to import: {exc}"
        ) from exc
    return module


def _provider_module_name(path: Path) -> str:
    return (
        "yoke_external_providers_"
        + hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:16]
    )


def _attach_plugin_model_catalog(
    provider: Provider,
    *,
    plugin: LoadedProviderPlugin,
    context: ProviderPluginContext,
) -> None:
    if isinstance(provider, ModelCatalogProvider):
        _normalize_plugin_provider_config(
            provider,
            provider.list_models(),
            reasoning_effort=context.reasoning_effort,
        )
        return
    if plugin.list_models is None:
        return
    model_catalog = plugin.list_models(replace(context, reasoning_effort=None))
    dynamic_provider = cast(Any, provider)
    dynamic_provider.provider_name = plugin.name
    dynamic_provider.list_models = lambda: [
        model.model_copy(deep=True) for model in model_catalog
    ]
    dynamic_provider.current_model_id = lambda: _current_plugin_model_id(provider)
    dynamic_provider.current_model_info = lambda: _current_plugin_model_info(
        provider, model_catalog
    )
    dynamic_provider.set_model = lambda model_id, reasoning_effort=None: (
        _set_plugin_model(
            provider,
            model_catalog,
            model_id,
            reasoning_effort=reasoning_effort,
        )
    )
    _normalize_plugin_provider_config(
        provider,
        model_catalog,
        reasoning_effort=context.reasoning_effort,
    )


def _normalize_plugin_provider_config(
    provider: Provider,
    model_catalog: list[ProviderModelInfo],
    *,
    reasoning_effort: str | None,
) -> None:
    current_model = _current_plugin_model_id(provider)
    config = getattr(provider, "config", None)
    if current_model is None or config is None:
        return
    set_config_model_from_catalog(
        config,
        model_catalog,
        provider_name=getattr(provider, "provider_name", "custom"),
        model_id=current_model,
        reasoning_effort=reasoning_effort,
    )


def _current_plugin_model_id(provider: Provider) -> str | None:
    config = getattr(provider, "config", None)
    model = getattr(config, "model", None)
    if isinstance(model, str) and model.strip():
        return model.strip()
    return None


def _current_plugin_model_info(
    provider: Provider,
    model_catalog: list[ProviderModelInfo],
) -> ProviderModelInfo | None:
    current_model = _current_plugin_model_id(provider)
    if current_model is None:
        return None
    for model in model_catalog:
        if model.id == current_model:
            return model.model_copy(deep=True)
    return None


def _set_plugin_model(
    provider: Provider,
    model_catalog: list[ProviderModelInfo],
    model_id: str,
    *,
    reasoning_effort: str | None = None,
) -> None:
    config = getattr(provider, "config", None)
    if config is None:
        raise ValueError("Provider does not expose a mutable config.model")
    set_config_model_from_catalog(
        config,
        model_catalog,
        provider_name=getattr(provider, "provider_name", "custom"),
        model_id=model_id,
        reasoning_effort=reasoning_effort,
    )
