"""Provider catalog helpers for CLI model selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yoke.ai.providers.base import ProviderModelInfo
from yoke.cli.config import CLIArgs
from yoke.cli.config.providers import BUILTIN_PROVIDER_NAMES
from yoke.cli.config.providers import list_builtin_provider_models
from yoke.cli.providers import available_custom_provider_names
from yoke.cli.providers import list_custom_provider_models


@dataclass(slots=True, frozen=True)
class ProviderModelChoice:
    """One provider-qualified model choice exposed by the CLI."""

    provider_name: str
    model: ProviderModelInfo

    @property
    def qualified_id(self) -> str:
        """Return the provider-qualified model identifier."""
        return f"{self.provider_name}:{self.model.id}"


def parse_provider_model_identifier(value: str) -> tuple[str, str]:
    """Parse `provider:model` from interactive input."""
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


def list_all_provider_model_choices(
    *,
    args: CLIArgs,
    home: Path | None = None,
) -> list[ProviderModelChoice]:
    """Return provider-qualified model choices across all providers."""
    choices: list[ProviderModelChoice] = []
    for provider_name in BUILTIN_PROVIDER_NAMES:
        models = list_builtin_provider_models(
            provider_name,
            reasoning_effort=args.reasoning_effort,
            home=home,
        )
        if models is None:
            continue
        choices.extend(
            ProviderModelChoice(provider_name=provider_name, model=model)
            for model in models
        )
    for provider_name in available_custom_provider_names(home=home):
        models = list_custom_provider_models(
            provider_name,
            reasoning_effort=args.reasoning_effort,
            home=home,
        )
        if models is None:
            continue
        choices.extend(
            ProviderModelChoice(provider_name=provider_name, model=model)
            for model in models
        )
    return sorted(
        choices,
        key=lambda item: (
            item.provider_name,
            item.model.display_name,
            item.model.id,
        ),
    )


def provider_qualified_model_choices(
    *,
    args: CLIArgs,
    home: Path | None = None,
) -> list[str]:
    """Return provider-qualified model ids for prompt completion."""
    return [
        choice.qualified_id
        for choice in list_all_provider_model_choices(args=args, home=home)
    ]
