from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from yoke.cli.config import CLIArgs
    from yoke.cli.main import app
    from yoke.cli.main import main
    from yoke.cli.runtime import run_cli


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Load CLI exports only when callers request them."""
    if name == "CLIArgs":
        from yoke.cli.config import CLIArgs

        globals()[name] = CLIArgs
        return CLIArgs
    if name in {"app", "main"}:
        from yoke.cli import main as main_module

        value = getattr(main_module, name)
        globals()[name] = value
        return value
    if name == "run_cli":
        from yoke.cli.runtime import run_cli

        globals()[name] = run_cli
        return run_cli
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["CLIArgs", "app", "main", "run_cli"]
