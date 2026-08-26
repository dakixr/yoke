"""Default skill discovery paths shared by application frontends."""

from __future__ import annotations

from pathlib import Path


def default_skill_dirs(root: Path, *, home: Path | None = None) -> list[str]:
    """Return existing repo and user skill directories for one root."""
    resolved_root = root.resolve()
    resolved_home = (home or Path.home()).resolve()
    candidates = {
        resolved_root / ".yoke" / "skills",
        resolved_home / ".yoke" / "skills",
    }
    return [str(path.resolve()) for path in candidates if path.is_dir()]
