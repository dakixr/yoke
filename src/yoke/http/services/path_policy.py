"""Filesystem containment policy for privileged daemon endpoints."""

from __future__ import annotations

from pathlib import Path

from yoke.http.errors import ApiError


class PathPolicy:
    """Resolve client paths while preventing traversal and symlink escapes."""

    def root(self, directory: str | None) -> Path:
        root = Path(directory or Path.cwd()).expanduser().resolve()
        if not root.is_dir():
            raise ApiError(
                404, "location_not_found", "Location directory was not found."
            )
        return root

    def contained(
        self,
        root: Path,
        relative_path: str | None,
        *,
        require_exists: bool = True,
    ) -> Path:
        raw = relative_path or "."
        requested = Path(raw)
        if requested.is_absolute():
            raise ApiError(
                403,
                "path_outside_location",
                "Filesystem paths must be relative to the requested location.",
            )
        candidate = (root / requested).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ApiError(
                403,
                "path_outside_location",
                "Filesystem path escapes the requested location.",
            ) from exc
        if require_exists and not candidate.exists():
            raise ApiError(404, "path_not_found", "Filesystem path was not found.")
        return candidate
