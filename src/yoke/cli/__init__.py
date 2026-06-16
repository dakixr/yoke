from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.cli.config.args import CLIArgs as CLIArgs
    from yoke.cli.main import app as app
    from yoke.cli.main import main as main
    from yoke.cli.runtime import run_cli as run_cli

_LAZY_EXPORTS = {
    "CLIArgs": ("yoke.cli.config.args", "CLIArgs"),
    "app": ("yoke.cli.main", "app"),
    "main": ("yoke.cli.main", "main"),
    "run_cli": ("yoke.cli.runtime", "run_cli"),
}

__all__ = ["CLIArgs", "app", "main", "run_cli"]


def __getattr__(name: str) -> Any:
    """Lazily resolve CLI package re-exports."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
