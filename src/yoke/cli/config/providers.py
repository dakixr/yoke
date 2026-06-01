"""Provider selection and construction for the yoke CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.codex_subscription import (
    list_provider_models as list_codex_models,
)
from yoke.ai.providers.codex_subscription import (
    register_provider as register_codex_provider,
)
from yoke.ai.providers.github_copilot_subscription import (
    list_provider_models as list_copilot_models,
)
from yoke.ai.providers.github_copilot_subscription import (
    register_provider as register_copilot_provider,
)
from yoke.ai.providers.opencode_go import (
    list_provider_models as list_opencode_go_models,
)
from yoke.ai.providers.opencode_go import (
    register_provider as register_opencode_go_provider,
)
from yoke.ai.providers.zai import list_provider_models as list_zai_models
from yoke.ai.providers.zai import register_provider as register_zai_provider
from yoke.cli.config.default_model import load_effective_yoke_config
from yoke.cli.config.default_model import parse_config_default_model
from yoke.cli.providers import available_custom_provider_names
from yoke.cli.providers import create_custom_provider
from yoke.cli.providers.registry import ProviderPluginContext

if TYPE_CHECKING:
    from collections.abc import Callable

    from yoke.cli.config.runtime import CLIArgs

    BuiltinProviderFactory = Callable[[ProviderPluginContext], Provider]
    BuiltinModelLister = Callable[[ProviderPluginContext], list[ProviderModelInfo]]

BUILTIN_PROVIDER_NAMES = ("codex", "copilot", "opencode-go", "zai")
_BUILTIN_PROVIDER_FACTORIES: dict[str, BuiltinProviderFactory] = {
    "codex": register_codex_provider,
    "copilot": register_copilot_provider,
    "opencode-go": register_opencode_go_provider,
    "zai": register_zai_provider,
}
_BUILTIN_MODEL_LISTERS: dict[str, BuiltinModelLister] = {
    "codex": list_codex_models,
    "copilot": list_copilot_models,
    "opencode-go": list_opencode_go_models,
    "zai": list_zai_models,
}


def prepare_provider_args(args: CLIArgs) -> None:
    """Apply default model config and split provider-qualified models."""
    _apply_config_default_model(args)
    _apply_config_default_reasoning_effort(args)
    _normalize_provider_model_args(args)


def build_provider_from_args(args: CLIArgs) -> Provider:
    """Build the selected provider from CLI args and environment."""
    try:
        provider_name = _resolve_provider_name(args)
    except ValueError as exc:
        provider = _build_first_available_provider(args)
        if provider is not None:
            return provider
        raise exc

    if provider_name in BUILTIN_PROVIDER_NAMES:
        return _build_builtin_provider(provider_name, args)

    custom_provider = create_custom_provider(
        provider_name,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    if custom_provider is not None:
        return custom_provider
    available = ", ".join(_available_provider_names())
    raise ValueError(
        f"Unsupported provider {provider_name!r}. Supported providers: {available}."
    )


def list_builtin_provider_models(
    provider_name: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    home: Path | None = None,
) -> list[ProviderModelInfo] | None:
    """Return a built-in provider model catalog."""
    normalized = provider_name.strip().lower()
    lister = _BUILTIN_MODEL_LISTERS.get(normalized)
    if lister is None:
        return None
    context = _provider_context(
        normalized,
        model=model,
        reasoning_effort=reasoning_effort,
        home=home,
    )
    return [model_info.model_copy(deep=True) for model_info in lister(context)]


def _build_builtin_provider(provider_name: str, args: CLIArgs) -> Provider:
    factory = _BUILTIN_PROVIDER_FACTORIES[provider_name]
    context = _provider_context(
        provider_name,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
    )
    try:
        return factory(context)
    except Exception as exc:
        raise ValueError(
            f"Could not initialize provider `{provider_name}`: {exc}"
        ) from exc


def _provider_context(
    provider_name: str,
    *,
    model: str | None,
    reasoning_effort: str | None,
    home: Path | None = None,
) -> ProviderPluginContext:
    return ProviderPluginContext(
        name=provider_name,
        home=(home or Path.home()).resolve(),
        model=model,
        reasoning_effort=reasoning_effort,
        env=os.environ,
    )


def _apply_config_default_model(args: CLIArgs) -> None:
    if args.model is not None:
        return
    config = load_effective_yoke_config(root=Path(args.root))
    default_model = parse_config_default_model(config.default_model)
    if default_model is None:
        return
    args.model = f"{default_model.provider_name}:{default_model.model_name}"
    args.provider_from_default = True


def _apply_config_default_reasoning_effort(args: CLIArgs) -> None:
    if args.reasoning_effort is not None:
        return
    config = load_effective_yoke_config(root=Path(args.root))
    if config.default_reasoning_effort is None:
        return
    args.reasoning_effort = config.default_reasoning_effort


def _normalize_provider_model_args(args: CLIArgs) -> None:
    model = args.model
    if not isinstance(model, str) or not model.strip():
        args.model = None
        return
    normalized = model.strip()
    if ":" not in normalized:
        args.model = normalized
        return
    provider_name, model_name = _parse_cli_provider_model_identifier(normalized)
    args.model = model_name
    args.provider_name = provider_name


def _parse_cli_provider_model_identifier(value: str) -> tuple[str, str]:
    normalized = value.strip()
    if ":" not in normalized:
        raise ValueError("Expected `provider-name:model-name` separated by `:`.")
    provider_name, model_id = normalized.split(":", maxsplit=1)
    provider_name = provider_name.strip().lower()
    model_id = model_id.strip()
    if not provider_name or not model_id:
        raise ValueError(
            "Expected `provider-name:model-name` with both parts non-empty."
        )
    return provider_name, model_id


def _resolve_provider_name(args: CLIArgs) -> str:
    configured_provider = args.provider_name
    if isinstance(configured_provider, str):
        provider = configured_provider.strip().lower()
        if provider:
            return provider
    if _has_codex_auth():
        return "codex"
    if _has_copilot_auth():
        return "copilot"
    if os.getenv("OPENCODE_API_KEY"):
        return "opencode-go"
    if os.getenv("ZAI_API_KEY"):
        return "zai"
    raise ValueError(
        "No provider credentials found. Configure a Codex/Copilot auth file, "
        "set OPENCODE_API_KEY or ZAI_API_KEY, or configure a custom provider."
    )


def _has_codex_auth() -> bool:
    return (Path.home() / ".codex" / "auth.json").is_file()


def _has_copilot_auth() -> bool:
    if os.getenv("YOKE_COPILOT_AUTH_PATH"):
        return True
    return (Path.home() / ".yoke" / "auth.json").is_file()


def _available_provider_names() -> list[str]:
    return sorted({*BUILTIN_PROVIDER_NAMES, *available_custom_provider_names()})


def _build_first_available_provider(args: CLIArgs) -> Provider | None:
    for provider_name in BUILTIN_PROVIDER_NAMES:
        try:
            return _build_builtin_provider(provider_name, args)
        except ValueError:
            continue
    return _build_first_available_custom_provider(args)


def _build_first_available_custom_provider(
    args: CLIArgs,
    *,
    exclude: set[str] | None = None,
) -> Provider | None:
    excluded = exclude or set()
    for provider_name in available_custom_provider_names():
        if provider_name in excluded:
            continue
        try:
            provider = create_custom_provider(
                provider_name,
                model=None,
                reasoning_effort=args.reasoning_effort,
            )
        except ValueError:
            continue
        if provider is not None:
            return provider
    return None
