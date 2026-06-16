"""CLI config helpers and runtime construction exports."""

from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.cli.config.args import CLIArgs as CLIArgs
    from yoke.cli.config.default_model import ConfigDefaultModel as ConfigDefaultModel
    from yoke.cli.config.default_model import (
        load_effective_yoke_config as load_effective_yoke_config,
    )
    from yoke.cli.config.default_model import (
        parse_config_default_model as parse_config_default_model,
    )
    from yoke.cli.config.providers import (
        BUILTIN_PROVIDER_NAMES as BUILTIN_PROVIDER_NAMES,
    )
    from yoke.cli.config.runtime import DEFAULT_SYSTEM_PROMPT as DEFAULT_SYSTEM_PROMPT
    from yoke.cli.config.runtime import RUN_ERRORS as RUN_ERRORS
    from yoke.cli.config.runtime import BuiltCLIAgent as BuiltCLIAgent
    from yoke.cli.config.runtime import build_agent_from_args as build_agent_from_args
    from yoke.cli.config.runtime import (
        build_cli_agent_from_args as build_cli_agent_from_args,
    )
    from yoke.cli.config.runtime import build_tool_report as build_tool_report
    from yoke.cli.config.runtime import default_cli_skill_dirs as default_cli_skill_dirs
    from yoke.cli.config.runtime import (
        format_provider_model_status as format_provider_model_status,
    )
    from yoke.cli.config.runtime import (
        format_tool_discovery_message as format_tool_discovery_message,
    )

_LAZY_EXPORTS = {
    "BUILTIN_PROVIDER_NAMES": ("yoke.cli.config.providers", "BUILTIN_PROVIDER_NAMES"),
    "BuiltCLIAgent": ("yoke.cli.config.runtime", "BuiltCLIAgent"),
    "CLIArgs": ("yoke.cli.config.args", "CLIArgs"),
    "ConfigDefaultModel": ("yoke.cli.config.default_model", "ConfigDefaultModel"),
    "DEFAULT_SYSTEM_PROMPT": ("yoke.cli.config.runtime", "DEFAULT_SYSTEM_PROMPT"),
    "RUN_ERRORS": ("yoke.cli.config.runtime", "RUN_ERRORS"),
    "build_agent_from_args": ("yoke.cli.config.runtime", "build_agent_from_args"),
    "build_cli_agent_from_args": (
        "yoke.cli.config.runtime",
        "build_cli_agent_from_args",
    ),
    "build_tool_report": ("yoke.cli.config.runtime", "build_tool_report"),
    "default_cli_skill_dirs": ("yoke.cli.config.runtime", "default_cli_skill_dirs"),
    "format_provider_model_status": (
        "yoke.cli.config.runtime",
        "format_provider_model_status",
    ),
    "format_tool_discovery_message": (
        "yoke.cli.config.runtime",
        "format_tool_discovery_message",
    ),
    "load_effective_yoke_config": (
        "yoke.cli.config.default_model",
        "load_effective_yoke_config",
    ),
    "parse_config_default_model": (
        "yoke.cli.config.default_model",
        "parse_config_default_model",
    ),
}

__all__ = [
    "BUILTIN_PROVIDER_NAMES",
    "BuiltCLIAgent",
    "CLIArgs",
    "ConfigDefaultModel",
    "DEFAULT_SYSTEM_PROMPT",
    "RUN_ERRORS",
    "build_agent_from_args",
    "build_cli_agent_from_args",
    "build_tool_report",
    "default_cli_skill_dirs",
    "format_provider_model_status",
    "format_tool_discovery_message",
    "load_effective_yoke_config",
    "parse_config_default_model",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve config package re-exports."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
