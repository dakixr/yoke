"""Helpers for config-driven default provider/model selection."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from yoke.cli.tools.policy import PiConfig
from yoke.cli.tools.policy import default_yoke_config
from yoke.cli.tools.policy import load_global_config
from yoke.cli.tools.policy import load_workspace_config
from yoke.cli.tools.policy import merge_configs


@dataclass(slots=True, frozen=True)
class ConfigDefaultModel:
    """Parsed provider/model pair from config."""

    provider_name: str
    model_name: str


def parse_config_default_model(value: str | None) -> ConfigDefaultModel | None:
    """Parse a validated `provider:model` config value."""
    if value is None:
        return None
    provider_name, model_name = value.split(":", maxsplit=1)
    return ConfigDefaultModel(
        provider_name=provider_name.strip().lower(),
        model_name=model_name.strip(),
    )


def load_effective_yoke_config(*, root: Path, home: Path | None = None) -> PiConfig:
    """Load the merged yoke config used by CLI startup."""
    resolved_home = (home or Path.home()).resolve()
    return merge_configs(
        default_yoke_config(),
        load_global_config(resolved_home).config,
        load_workspace_config(root.resolve()).config,
    )
