"""Tool for direct UTF-8 file writes."""

from __future__ import annotations

from pydantic import Field

from .base import WorkspaceTool


class WriteTool(WorkspaceTool):
    """Write a UTF-8 text file in the workspace."""

    name = "write"
    description = (
        "Write UTF-8 text content to a file under the workspace root. "
        "Set overwrite=true to replace an existing file. Set createDirs=true "
        "to create missing parent directories."
    )

    path: str = Field(min_length=1)
    content: str
    overwrite: bool = False
    create_dirs: bool = Field(default=False, alias="createDirs")

    def execute(self) -> dict[str, object]:
        """Write the requested file content."""
        try:
            path = self._resolve_path(self.path, allow_missing=True)
            existed = path.exists()
            if existed and not path.is_file():
                return self._error(
                    f"Path is not a regular file: {self.path}",
                    path=self.path,
                )
            if existed and not self.overwrite:
                return self._error(
                    "File already exists. Set overwrite=true to replace it.",
                    path=self.path,
                )
            if not path.parent.exists():
                if not self.create_dirs:
                    return self._error(
                        "Parent directory does not exist. Set createDirs=true "
                        "to create it.",
                        path=self.path,
                    )
                path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self.content, encoding="utf-8")
            return self._success(
                path=self._display_path(path),
                created=not existed,
                bytes_written=len(self.content.encode("utf-8")),
            )
        except Exception as exc:
            return self._error(str(exc), path=self.path)
