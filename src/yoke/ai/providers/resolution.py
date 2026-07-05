"""Public provider readiness and construction helpers."""

from __future__ import annotations

import os
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import cast

from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.cli.providers import available_custom_provider_names
from yoke.cli.providers import create_custom_provider
from yoke.cli.providers import list_custom_provider_models
from yoke.cli.providers.registry import ProviderPluginContext


BUILTIN_PROVIDER_NAMES = (
    "codex",
    "opencode-go",
    "zai",
)
_BUILTIN_PROVIDER_TARGETS = {
    "codex": ("yoke.ai.providers.codex.websockets", "register_provider"),
    "opencode-go": ("yoke.ai.providers.opencode_go", "register_provider"),
    "zai": ("yoke.ai.providers.zai", "register_provider"),
}
_BUILTIN_MODEL_LISTER_TARGETS = {
    "codex": ("yoke.ai.providers.codex.websockets", "list_provider_models"),
    "opencode-go": ("yoke.ai.providers.opencode_go", "list_provider_models"),
    "zai": ("yoke.ai.providers.zai", "list_provider_models"),
}

type ProviderFactory = Callable[[ProviderPluginContext], Provider]
type ModelLister = Callable[[ProviderPluginContext], list[ProviderModelInfo]]


@dataclass(slots=True, frozen=True)
class ProviderRef:
    """Parsed provider reference."""

    provider_name: str
    model: str | None = None
    reasoning_effort: str | None = None

    @property
    def qualified_name(self) -> str:
        """Return `provider:model:thinking` with missing parts omitted."""
        parts = [self.provider_name]
        if self.model is not None:
            parts.append(self.model)
        if self.reasoning_effort is not None:
            parts.append(self.reasoning_effort)
        return ":".join(parts)


@dataclass(slots=True, frozen=True)
class ProviderReadiness:
    """Constructability and catalog status for one provider in one environment."""

    provider_name: str
    ready: bool
    reason: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    models: tuple[ProviderModelInfo, ...] = ()


def parse_provider_ref(value: str) -> ProviderRef:
    """Parse `provider`, `provider:model`, or `provider:model:thinking`."""
    parts = [part.strip() for part in value.split(":")]
    if len(parts) > 3:
        raise ValueError(
            "Expected `provider`, `provider:model`, or "
            "`provider:model:thinking-effort`."
        )
    provider_name = parts[0].lower() if parts else ""
    if not provider_name:
        raise ValueError("Provider name must be non-empty.")
    model = _normalized_optional(parts[1]) if len(parts) >= 2 else None
    reasoning_effort = (
        _normalized_optional(parts[2].lower()) if len(parts) == 3 else None
    )
    if len(parts) == 3 and model is None:
        raise ValueError("Provider reference cannot include thinking without model.")
    return ProviderRef(
        provider_name=provider_name,
        model=model,
        reasoning_effort=reasoning_effort,
    )


def build_provider(
    qualified_name: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> Provider:
    """Build a provider from `provider:model:thinking-effort`."""
    provider_ref = parse_provider_ref(qualified_name)
    resolved_env = os.environ if env is None else env
    resolved_home = _resolved_home(home)
    credential_issue = _credential_issue(
        provider_ref.provider_name,
        env=resolved_env,
        home=resolved_home,
    )
    if credential_issue is not None:
        raise ValueError(credential_issue)
    if provider_ref.provider_name in BUILTIN_PROVIDER_NAMES:
        return _build_builtin_provider(
            provider_ref, env=resolved_env, home=resolved_home
        )
    provider = create_custom_provider(
        provider_ref.provider_name,
        model=provider_ref.model,
        reasoning_effort=provider_ref.reasoning_effort,
        home=resolved_home,
        env=resolved_env,
    )
    if provider is None:
        raise ValueError(
            f"Unsupported provider {provider_ref.provider_name!r}. "
            f"Supported providers: {', '.join(available_provider_names(home=resolved_home))}."
        )
    return provider


def provider_readiness(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> list[ProviderReadiness]:
    """Report readiness for all known providers in this env."""
    resolved_home = _resolved_home(home)
    return [
        provider_status(provider_name, env=env, home=resolved_home)
        for provider_name in available_provider_names(home=resolved_home)
    ]


def provider_status(
    qualified_name: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> ProviderReadiness:
    """Report whether one provider reference can be constructed in this env."""
    try:
        provider_ref = parse_provider_ref(qualified_name)
    except ValueError as exc:
        return ProviderReadiness(
            provider_name=qualified_name, ready=False, reason=str(exc)
        )
    resolved_env = os.environ if env is None else env
    resolved_home = _resolved_home(home)
    models = tuple(
        list_provider_models(
            provider_ref.provider_name,
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
            env=resolved_env,
            home=resolved_home,
        )
        or []
    )
    credential_issue = _credential_issue(
        provider_ref.provider_name,
        env=resolved_env,
        home=resolved_home,
    )
    if credential_issue is not None:
        return ProviderReadiness(
            provider_name=provider_ref.provider_name,
            ready=False,
            reason=credential_issue,
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
            models=models,
        )
    try:
        provider = build_provider(
            provider_ref.qualified_name, env=resolved_env, home=resolved_home
        )
    except Exception as exc:
        return ProviderReadiness(
            provider_name=provider_ref.provider_name,
            ready=False,
            reason=str(exc),
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
            models=models,
        )
    close = getattr(provider, "close", None)
    if callable(close):
        close()
    return ProviderReadiness(
        provider_name=provider_ref.provider_name,
        ready=True,
        model=_current_model(provider) or provider_ref.model,
        reasoning_effort=_current_reasoning_effort(provider)
        or provider_ref.reasoning_effort,
        models=models,
    )


def list_provider_readiness(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> list[ProviderReadiness]:
    """Report readiness for all known built-in and custom providers."""
    return provider_readiness(env=env, home=home)


def is_provider_ready(
    qualified_name: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> bool:
    """Return whether one provider reference is ready in this env."""
    return provider_status(qualified_name, env=env, home=home).ready


def available_provider_names(*, home: Path | str | None = None) -> list[str]:
    """Return known built-in and custom provider names."""
    resolved_home = _resolved_home(home)
    return sorted(
        {
            *BUILTIN_PROVIDER_NAMES,
            *available_custom_provider_names(home=resolved_home),
        }
    )


def list_provider_models(
    provider_name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> list[ProviderModelInfo] | None:
    """Return model metadata for a built-in or custom provider."""
    normalized = provider_name.strip().lower()
    resolved_env = os.environ if env is None else env
    resolved_home = _resolved_home(home)
    lister = _builtin_model_lister(normalized)
    if lister is not None:
        return [
            item.model_copy(deep=True)
            for item in lister(
                _provider_context(
                    normalized,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    env=resolved_env,
                    home=resolved_home,
                )
            )
        ]
    return list_custom_provider_models(
        normalized,
        model=model,
        reasoning_effort=reasoning_effort,
        home=resolved_home,
        env=resolved_env,
    )


def _build_builtin_provider(
    provider_ref: ProviderRef,
    *,
    env: Mapping[str, str],
    home: Path,
) -> Provider:
    factory = _builtin_provider_factory(provider_ref.provider_name)
    if factory is None:
        raise ValueError(
            f"Unsupported built-in provider {provider_ref.provider_name!r}."
        )
    return factory(
        _provider_context(
            provider_ref.provider_name,
            model=provider_ref.model,
            reasoning_effort=provider_ref.reasoning_effort,
            env=env,
            home=home,
        )
    )


def _provider_context(
    provider_name: str,
    *,
    model: str | None,
    reasoning_effort: str | None,
    env: Mapping[str, str],
    home: Path,
) -> ProviderPluginContext:
    return ProviderPluginContext(
        name=provider_name,
        home=home.resolve(),
        model=model,
        reasoning_effort=reasoning_effort,
        env=env,
    )


def _credential_issue(
    provider_name: str,
    *,
    env: Mapping[str, str],
    home: Path,
) -> str | None:
    if provider_name == "zai" and not env.get("ZAI_API_KEY"):
        return "zai provider requires ZAI_API_KEY."
    if provider_name == "opencode-go" and not env.get("OPENCODE_API_KEY"):
        return "opencode-go provider requires OPENCODE_API_KEY."
    if provider_name != "codex":
        return None
    if env.get("YOKE_CODEX_API_KEY"):
        return None
    if (home / ".codex" / "auth.json").is_file():
        return None
    auths_path = (
        Path(env["YOKE_CODEX_AUTHS_PATH"])
        if env.get("YOKE_CODEX_AUTHS_PATH")
        else home / ".yoke" / "providers" / "codex-auth" / "auths.json"
    )
    if auths_path.is_file():
        return None
    return "codex provider requires YOKE_CODEX_API_KEY or stored Codex auth."


def _builtin_provider_factory(provider_name: str) -> ProviderFactory | None:
    target = _BUILTIN_PROVIDER_TARGETS.get(provider_name)
    if target is None:
        return None
    module_name, attribute = target
    return cast(ProviderFactory, getattr(import_module(module_name), attribute))


def _builtin_model_lister(provider_name: str) -> ModelLister | None:
    target = _BUILTIN_MODEL_LISTER_TARGETS.get(provider_name)
    if target is None:
        return None
    module_name, attribute = target
    return cast(ModelLister, getattr(import_module(module_name), attribute))


def _current_model(provider: Provider) -> str | None:
    config = getattr(provider, "config", None)
    model = getattr(config, "model", None)
    return model.strip() if isinstance(model, str) and model.strip() else None


def _current_reasoning_effort(provider: Provider) -> str | None:
    config = getattr(provider, "config", None)
    effort = getattr(config, "reasoning_effort", None)
    return effort.strip() if isinstance(effort, str) and effort.strip() else None


def _resolved_home(home: Path | str | None) -> Path:
    return (Path.home() if home is None else Path(home)).resolve()


def _normalized_optional(value: str) -> str | None:
    normalized = value.strip()
    return normalized or None
