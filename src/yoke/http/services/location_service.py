"""Explicit workspace resolution for global catalog and session creation calls."""

from __future__ import annotations

from pathlib import Path
import os
import subprocess
from threading import Lock
import time

from yoke.http.errors import ApiError
from yoke.http.models.common import LocationInfo
from yoke.http.models.location import GitLocationInfo
from yoke.http.models.location import LocationBrowseEntry
from yoke.http.models.location import LocationBrowseResult
from yoke.http.models.location import ResolvedLocation
from yoke.session import SessionStore


class LocationService:
    """Resolve authorized local workspace roots without relying on process cwd."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self._cache_lock = Lock()
        self._cache: dict[str, tuple[float, ResolvedLocation]] = {}

    def resolve(self, directory: str | None) -> ResolvedLocation:
        path = Path(directory or Path.cwd()).expanduser().resolve()
        if not path.exists() or not path.is_dir():
            raise ApiError(
                404, "location_not_found", "Workspace directory was not found."
            )
        key = str(path)
        now = time.monotonic()
        with self._cache_lock:
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < 5:
                return cached[1].model_copy(deep=True)
        git = self._git_info(path)
        resolved = ResolvedLocation(
            directory=str(path),
            name=path.name or str(path),
            git=git,
        )
        with self._cache_lock:
            self._cache[key] = (now, resolved)
            if len(self._cache) > 512:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                self._cache.pop(oldest, None)
        return resolved.model_copy(deep=True)

    def recent(self) -> list[LocationInfo]:
        seen: set[str] = set()
        result: list[LocationInfo] = []
        for entry in self.store.list_index_entries(maintain=False):
            if not entry.root:
                continue
            root = entry.root
            if root in seen:
                continue
            seen.add(root)
            result.append(LocationInfo(directory=root))
        return result

    def browse(self, partial_path: str, *, limit: int) -> LocationBrowseResult:
        raw = partial_path.strip()
        if not raw:
            raise ApiError(
                400, "location_path_required", "Enter a location path to browse."
            )

        expanded = Path(raw).expanduser()
        if not expanded.is_absolute():
            raise ApiError(
                400,
                "location_path_not_absolute",
                "Location browsing requires an absolute path or a path starting with '~'.",
            )

        candidate = expanded.resolve(strict=False)
        has_trailing_separator = raw.endswith(("/", "\\")) or raw == "~"
        has_hidden_prefix = raw.endswith(("/.", "\\."))
        if has_hidden_prefix:
            browse_directory = candidate
            prefix = "."
        elif has_trailing_separator:
            browse_directory = candidate
            prefix = ""
        else:
            browse_directory = candidate.parent
            prefix = candidate.name

        if not browse_directory.is_dir():
            raise ApiError(
                404,
                "location_parent_not_found",
                "The parent directory for this location was not found.",
            )

        try:
            children = sorted(
                browse_directory.iterdir(),
                key=lambda item: item.name.casefold(),
            )
        except OSError as exc:
            raise ApiError(
                403,
                "location_parent_unreadable",
                "The parent directory for this location cannot be read.",
            ) from exc

        needle = prefix.casefold()
        show_hidden = prefix.startswith(".")
        entries: list[LocationBrowseEntry] = []
        for child in children:
            name = child.name
            if not show_hidden and name.startswith("."):
                continue
            if needle and not name.casefold().startswith(needle):
                continue
            try:
                if not child.is_dir():
                    continue
            except OSError:
                continue
            entries.append(
                LocationBrowseEntry(
                    name=name,
                    directory=str(child.resolve(strict=False)),
                )
            )
            if len(entries) >= limit:
                break

        selectable_directory = str(candidate) if candidate.is_dir() else None
        parent = browse_directory.parent
        parent_directory = None if parent == browse_directory else str(parent)
        return LocationBrowseResult(
            browse_directory=str(browse_directory),
            parent_directory=parent_directory,
            selectable_directory=selectable_directory,
            separator=os.sep,
            entries=entries,
        )

    @staticmethod
    def _git_info(path: Path) -> GitLocationInfo | None:
        try:
            root = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return None
        branch: str | None = None
        try:
            branch_value = subprocess.run(
                ["git", "-C", str(path), "branch", "--show-current"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
            branch = branch_value or None
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        return GitLocationInfo(root=root, branch=branch)
