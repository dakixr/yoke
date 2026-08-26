"""Safe filesystem listing and recursive discovery."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from yoke.http.errors import ApiError
from yoke.http.models.common import LocationInfo
from yoke.http.models.filesystem import FileEntry
from yoke.http.models.filesystem import FileListResponse
from yoke.http.services.path_policy import PathPolicy


class FilesystemService:
    """Read location-contained filesystem metadata."""

    def __init__(self, policy: PathPolicy) -> None:
        self.policy = policy

    def list(
        self,
        *,
        directory: str | None,
        path: str | None,
    ) -> FileListResponse:
        root = self.policy.root(directory)
        target = self.policy.contained(root, path)
        if not target.is_dir():
            raise ApiError(400, "not_a_directory", "Filesystem path is not a directory.")
        try:
            children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold()))
        except OSError as exc:
            raise ApiError(403, "path_unreadable", "Filesystem path cannot be read.") from exc
        data: list[FileEntry] = []
        for child in children:
            try:
                resolved = child.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved.is_dir():
                kind: Literal["file", "directory"] = "directory"
                size = None
            elif resolved.is_file():
                kind = "file"
                try:
                    size = resolved.stat().st_size
                except OSError:
                    size = None
            else:
                continue
            data.append(
                FileEntry(
                    name=child.name,
                    path=resolved.relative_to(root).as_posix(),
                    type=kind,
                    size=size,
                )
            )
        return FileListResponse(
            location=LocationInfo(directory=str(root)),
            data=data,
        )

    def find(
        self,
        *,
        directory: str | None,
        query: str,
        entry_type: Literal["file", "directory"],
        limit: int,
    ) -> FileListResponse:
        root = self.policy.root(directory)
        needle = query.casefold().strip()
        results: list[FileEntry] = []
        for current, dirs, files in os.walk(root, followlinks=False):
            current_path = Path(current)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
            names = files if entry_type == "file" else dirs
            for name in sorted(names, key=str.casefold):
                path = (current_path / name).resolve(strict=False)
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    continue
                relative_text = relative.as_posix()
                if needle and needle not in relative_text.casefold():
                    continue
                size: int | None = None
                if entry_type == "file":
                    try:
                        size = path.stat().st_size
                    except OSError:
                        pass
                results.append(
                    FileEntry(
                        name=name,
                        path=relative_text,
                        type=entry_type,
                        size=size,
                    )
                )
                if len(results) >= limit:
                    return FileListResponse(
                        location=LocationInfo(directory=str(root)),
                        data=results,
                    )
        return FileListResponse(
            location=LocationInfo(directory=str(root)),
            data=results,
        )

    def readable_file(self, *, directory: str | None, path: str) -> tuple[Path, Path]:
        root = self.policy.root(directory)
        target = self.policy.contained(root, path)
        if not target.is_file():
            raise ApiError(400, "not_a_file", "Filesystem path is not a file.")
        return root, target
