"""Provider selection and construction for the yoke CLI."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.credentials import provider_environment
from yoke.ai.providers.plugins import ProviderPluginContext
from yoke.ai.providers.plugins import available_custom_provider_names
from yoke.ai.providers.plugins import create_custom_provider
from yoke.ai.providers.resolution import BUILTIN_PROVIDER_NAMES
from yoke.ai.providers.resolution import build_builtin_provider
from yoke.ai.providers.resolution import list_provider_models
from yoke.ai.providers.resolution import parse_provider_ref
from yoke.cli.config.default_model import load_effective_yoke_config
from yoke.cli.config.default_model import parse_config_default_model

if TYPE_CHECKING:
    from collections.abc import Callable

    from yoke.cli.config.runtime import CLIArgs

    BuiltinProviderFactory = Callable[[ProviderPluginContext], Provider]
    BuiltinModelLister = Callable[[ProviderPluginContext], list[ProviderModelInfo]]
else:
    from collections.abc import Callable

    BuiltinProviderFactory = Callable[[ProviderPluginContext], Provider]
    BuiltinModelLister = Callable[[ProviderPluginContext], list[ProviderModelInfo]]

# These override maps remain as explicit injection seams for embedders and tests.
_BUILTIN_PROVIDER_FACTORIES: dict[str, BuiltinProviderFactory] = {}
_BUILTIN_MODEL_LISTERS: dict[str, BuiltinModelLister] = {}


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
        session_id=args.session,
        home=Path.home(),
        env=_provider_env(),
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
    home: Path,
) -> list[ProviderModelInfo] | None:
    """Return a built-in provider model catalog."""
    normalized = provider_name.strip().lower()
    lister = _BUILTIN_MODEL_LISTERS.get(normalized)
    if lister is not None:
        context = _provider_context(
            normalized,
            model=model,
            reasoning_effort=reasoning_effort,
            home=home,
        )
        return [model_info.model_copy(deep=True) for model_info in lister(context)]
    return list_provider_models(
        normalized,
        model=model,
        reasoning_effort=reasoning_effort,
        home=home,
    )


def _build_builtin_provider(provider_name: str, args: CLIArgs) -> Provider:
    try:
        factory = _BUILTIN_PROVIDER_FACTORIES.get(provider_name)
        if factory is not None:
            return factory(
                _provider_context(
                    provider_name,
                    model=args.model,
                    reasoning_effort=args.reasoning_effort,
                    session_id=args.session,
                    home=Path.home(),
                )
            )
        return build_builtin_provider(
            provider_name,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            session_id=args.session,
            home=Path.home(),
        )
    except Exception as exc:
        raise ValueError(
            f"Could not initialize provider `{provider_name}`: {exc}"
        ) from exc


def _provider_context(
    provider_name: str,
    *,
    model: str | None,
    reasoning_effort: str | None,
    session_id: str | None = None,
    home: Path,
) -> ProviderPluginContext:
    return ProviderPluginContext(
        name=provider_name,
        home=home.resolve(),
        model=model,
        reasoning_effort=reasoning_effort,
        session_id=session_id,
        env=_provider_env(home=home),
    )


def _apply_config_default_model(args: CLIArgs) -> None:
    if args.model is not None:
        return
    config = load_effective_yoke_config(root=Path(args.root), home=Path.home())
    default_model = parse_config_default_model(config.default_model)
    if default_model is None:
        return
    args.model = f"{default_model.provider_name}:{default_model.model_name}"
    args.provider_from_default = True


def _apply_config_default_reasoning_effort(args: CLIArgs) -> None:
    if args.reasoning_effort is not None:
        return
    config = load_effective_yoke_config(root=Path(args.root), home=Path.home())
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
    provider_ref = parse_provider_ref(normalized)
    if provider_ref.model is None:
        raise ValueError("Expected `provider-name:model-name` with model name.")
    args.model = provider_ref.model
    args.provider_name = provider_ref.provider_name
    if args.reasoning_effort is None:
        args.reasoning_effort = provider_ref.reasoning_effort


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
    env = _provider_env()
    if _has_codex_auth():
        return "codex"
    if env.get("OPENCODE_API_KEY"):
        return "opencode-go"
    if env.get("ZAI_API_KEY"):
        return "zai"
    raise ValueError(
        "No provider credentials found. Configure a Codex auth file, "
        "set OPENCODE_API_KEY or ZAI_API_KEY, or configure a custom provider."
    )


def _has_codex_auth() -> bool:
    home = Path.home()
    env = _provider_env(home=home)
    if env.get("YOKE_CODEX_API_KEY"):
        return True
    if (home / ".codex" / "auth.json").is_file():
        return True
    if any((home / ".codex-auth" / "accounts").glob("*/auth.json")):
        return True
    auths_path = (
        Path(env["YOKE_CODEX_AUTHS_PATH"])
        if env.get("YOKE_CODEX_AUTHS_PATH")
        else home / ".yoke" / "providers" / "codex-auth" / "auths.json"
    )
    return auths_path.is_file()


def _available_provider_names() -> list[str]:
    return sorted(
        {
            *BUILTIN_PROVIDER_NAMES,
            *available_custom_provider_names(home=Path.home()),
        }
    )


def _build_first_available_provider(args: CLIArgs) -> Provider | None:
    return _build_first_available_custom_provider(args)


def _build_first_available_custom_provider(
    args: CLIArgs,
    *,
    exclude: set[str] | None = None,
) -> Provider | None:
    excluded = exclude or set()
    for provider_name in available_custom_provider_names(home=Path.home()):
        if provider_name in excluded:
            continue
        try:
            provider = create_custom_provider(
                provider_name,
                model=None,
                reasoning_effort=args.reasoning_effort,
                session_id=args.session,
                home=Path.home(),
                env=_provider_env(),
            )
        except ValueError:
            continue
        if provider is not None:
            return provider
    return None


def _provider_env(*, home: Path | None = None) -> dict[str, str]:
    resolved_home = Path.home() if home is None else home
    return provider_environment(home=resolved_home, env=os.environ)
