"""SDK provider discovery and construction with Yoke provider semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import default_reasoning_effort_for_model
from yoke.ai.providers.resolution import available_provider_names
from yoke.ai.providers.resolution import build_provider
from yoke.ai.providers.resolution import list_provider_models
from yoke.ai.providers.resolution import parse_provider_ref
from yoke.ai.providers.resolution import provider_status

DEFAULT_BUILTIN_PROVIDER_SELECTION = "codex:gpt-5.6-sol:medium"


@dataclass(frozen=True, slots=True)
class BuiltinProviderModelStatus:
    """Status for one provider/model combination."""

    provider_name: str
    model: ProviderModelInfo
    default_thinking_effort: str | None

    @property
    def selection(self) -> str:
        """Return the provider-qualified default selection."""
        base = f"{self.provider_name}:{self.model.id}"
        if self.default_thinking_effort is None:
            return base
        return f"{base}:{self.default_thinking_effort}"


@dataclass(frozen=True, slots=True)
class BuiltinProviderStatus:
    """Readiness and model metadata for one provider."""

    name: str
    ready: bool
    missing_env: tuple[str, ...]
    default_model: str
    default_selection: str
    models: tuple[BuiltinProviderModelStatus, ...]


def builtin_provider_status(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> tuple[BuiltinProviderStatus, ...]:
    """Return readiness and model metadata for every known provider."""
    resolved_home = (Path.home() if home is None else Path(home)).resolve()
    statuses: list[BuiltinProviderStatus] = []
    for name in available_provider_names(home=resolved_home):
        readiness = provider_status(name, env=env, home=resolved_home)
        models = tuple(list_provider_models(name, env=env, home=resolved_home) or ())
        model_statuses = tuple(
            BuiltinProviderModelStatus(
                provider_name=name,
                model=model.model_copy(deep=True),
                default_thinking_effort=default_reasoning_effort_for_model(model),
            )
            for model in models
        )
        default = _default_model(models, readiness.model)
        default_effort = (
            default_reasoning_effort_for_model(default) if default is not None else None
        )
        statuses.append(
            BuiltinProviderStatus(
                name=name,
                ready=readiness.ready,
                missing_env=(
                    () if readiness.ready else (readiness.reason or "unavailable",)
                ),
                default_model=default.id if default is not None else "",
                default_selection=_selection(name, default, default_effort),
                models=model_statuses,
            )
        )
    return tuple(statuses)


def print_builtin_provider_status() -> None:
    """Print provider readiness and selectable models."""
    statuses = builtin_provider_status()
    ready = [status for status in statuses if status.ready]
    unavailable = [status for status in statuses if not status.ready]
    print("Ready providers:" if ready else "Ready providers: none")
    for status in ready:
        _print_status(status)
    if unavailable:
        print("\nUnavailable providers:")
        for status in unavailable:
            print(f"- {status.name}: {', '.join(status.missing_env)}")


def build_builtin_provider(
    selection: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
    session_id: str | None = None,
) -> Provider:
    """Build a provider from ``provider:model:thinking_effort``."""
    return build_provider(
        selection or DEFAULT_BUILTIN_PROVIDER_SELECTION,
        env=env,
        home=home,
        session_id=session_id,
    )


def available_builtin_providers(
    selections: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | str | None = None,
) -> dict[str, Provider]:
    """Build every requested provider that is ready."""
    requested = tuple(selections or (DEFAULT_BUILTIN_PROVIDER_SELECTION,))
    providers: dict[str, Provider] = {}
    for selection in requested:
        try:
            provider = build_builtin_provider(selection, env=env, home=home)
        except ValueError:
            continue
        providers[parse_provider_ref(selection).qualified_name] = provider
    return providers


def _default_model(
    models: tuple[ProviderModelInfo, ...], configured: str | None
) -> ProviderModelInfo | None:
    if configured is not None:
        selected = next((model for model in models if model.id == configured), None)
        if selected is not None:
            return selected
    return models[0] if models else None


def _selection(
    name: str,
    model: ProviderModelInfo | None,
    effort: str | None,
) -> str:
    if model is None:
        return name
    base = f"{name}:{model.id}"
    return base if effort is None else f"{base}:{effort}"


def _print_status(status: BuiltinProviderStatus) -> None:
    print(f"- {status.name}: ready")
    print(f"  default selection: {status.default_selection}")
    print("  models:")
    for item in status.models:
        efforts = ", ".join(item.model.thinking_levels)
        print(f"    - {status.name}:{item.model.id}")
        print(f"      display: {item.model.display_name}")
        print(f"      thinking efforts: {efforts}")
        print(f"      default selection: {item.selection}")
