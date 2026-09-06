"""Default skill discovery paths shared by application frontends."""

from __future__ import annotations

from pathlib import Path


def default_skill_dirs(root: Path, *, home: Path | None = None) -> list[str]:
    """Return existing repo and user skill directories in precedence order."""
    resolved_root = root.resolve()
    resolved_home = (home or Path.home()).resolve()
    candidates = [
        resolved_root / ".yoke" / "skills",
        resolved_home / ".yoke" / "skills",
    ]
    discovered: list[str] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = candidate.resolve()
        if path in seen or not path.is_dir():
            continue
        seen.add(path)
        discovered.append(str(path))
    return discovered
