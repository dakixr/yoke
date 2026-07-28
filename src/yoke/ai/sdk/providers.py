"""SDK helpers for provider discovery and construction."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.model_selection import default_reasoning_effort_for_model
from yoke.ai.providers.resolution import build_provider
from yoke.ai.providers.resolution import parse_provider_ref
from yoke.ai.providers.resolution import provider_readiness


@dataclass(frozen=True)
class BuiltinProviderModelStatus:
    """Status for one provider/model combination."""

    provider_name: str
    model: ProviderModelInfo
    default_thinking_effort: str | None

    @property
    def selection(self) -> str:
        """Return the default provider:model:thinking selection."""
        base = f"{self.provider_name}:{self.model.id}"
        if self.default_thinking_effort is None:
            return base
        return f"{base}:{self.default_thinking_effort}"


@dataclass(frozen=True)
class BuiltinProviderStatus:
    """Readiness and model metadata for one provider."""

    name: str
    ready: bool
    missing_env: tuple[str, ...]
    default_model: str
    default_selection: str
    provider_import: str
    config_import: str
    models: tuple[BuiltinProviderModelStatus, ...]


def builtin_provider_status() -> tuple[BuiltinProviderStatus, ...]:
    """Return readiness and selectable model metadata for providers."""
    statuses: list[BuiltinProviderStatus] = []
    for readiness in provider_readiness():
        models = tuple(
            BuiltinProviderModelStatus(
                provider_name=readiness.provider_name,
                model=model.model_copy(deep=True),
                default_thinking_effort=default_reasoning_effort_for_model(model),
            )
            for model in readiness.models
        )
        default_model = readiness.model or (models[0].model.id if models else "")
        default_effort = readiness.reasoning_effort
        if default_effort is None and models:
            selected = next(
                (item for item in models if item.model.id == default_model), models[0]
            )
            default_effort = selected.default_thinking_effort
        default_selection = _selection(
            readiness.provider_name, default_model, default_effort
        )
        statuses.append(
            BuiltinProviderStatus(
                name=readiness.provider_name,
                ready=readiness.ready,
                missing_env=_missing_env_from_reason(readiness.reason),
                default_model=default_model,
                default_selection=default_selection,
                provider_import="",
                config_import="",
                models=models,
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
        print(f"- {status.name}: ready")
        print(f"  default selection: {status.default_selection}")
        print("  models:")
        for model in status.models:
            efforts = ", ".join(model.model.thinking_levels)
            print(f"    - {status.name}:{model.model.id}")
            print(f"      display: {model.model.display_name}")
            print(f"      thinking efforts: {efforts}")
            print(f"      default selection: {model.selection}")
    if unavailable:
        print("\nUnavailable providers:")
        for status in unavailable:
            detail = ", ".join(status.missing_env) or "not configured"
            print(f"- {status.name}: missing {detail}")


def build_builtin_provider(selection: str | None = None) -> Provider:
    """Build one provider from provider:model:thinking_effort."""
    selected = selection or _default_ready_selection()
    return build_provider(selected)


def available_builtin_providers(
    selections: Sequence[str] | None = None,
) -> dict[str, Provider]:
    """Build ready providers for requested selections."""
    requested = list(selections or [_default_ready_selection()])
    providers: dict[str, Provider] = {}
    for selection in requested:
        try:
            parsed = parse_provider_ref(selection)
            providers[parsed.qualified_name] = build_provider(selection)
        except ValueError:
            continue
    return providers


def _default_ready_selection() -> str:
    for status in builtin_provider_status():
        if status.ready:
            return status.default_selection or status.name
    raise ValueError("No locally ready Yoke provider is available.")


def _selection(provider: str, model: str, effort: str | None) -> str:
    if not model:
        return provider
    if effort is None:
        return f"{provider}:{model}"
    return f"{provider}:{model}:{effort}"


def _missing_env_from_reason(reason: str | None) -> tuple[str, ...]:
    if reason is None:
        return ()
    marker = " requires "
    if marker not in reason:
        return ()
    value = reason.split(marker, 1)[1].rstrip(".")
    return tuple(part.strip() for part in value.split(" or ") if part.strip())
