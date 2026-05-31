"""Path display helpers for CLI surfaces."""

from __future__ import annotations

from pathlib import Path


def format_root_label(root: Path) -> str:
    """Format a root path with a home-relative label when possible."""
    resolved = root.resolve()
    try:
        home = Path.home().resolve()
        relative = resolved.relative_to(home)
    except ValueError:
        return str(resolved)
    if not str(relative):
        return "~"
    return "~" + "\\" + str(relative).replace("/", "\\")
