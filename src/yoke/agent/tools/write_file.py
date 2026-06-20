"""Tool for writing complete text files."""

from __future__ import annotations

from pydantic import Field


from .base import WorkspaceTool
from .edit import decode_text
from .edit import encode_text


class WriteTool(WorkspaceTool):
    """Tool that writes complete file content, creating parent directories."""

    name = "write"
    description = "Write content to one file. Creates the file if it is missing."

    path: str = Field(min_length=1)
    content: str = Field(description="Content to write to the file")

    def execute(self) -> dict[str, object]:
        """Write the complete file content and return the result dict."""
        try:
            path = self._resolve_path(self.path, allow_missing=True)
            existed = path.exists()
            if existed and not path.is_file():
                return self._error(
                    f"Path is not a regular file: {self.path}",
                    path=self.path,
                )
            bom = False
            if existed:
                bom = decode_text(path.read_bytes()).bom
            path.parent.mkdir(parents=True, exist_ok=True)
            content = encode_text(self.content, bom=bom)
            path.write_bytes(content)
            return self._success(
                bytes_written=len(content),
                created=not existed,
            )
        except Exception as exc:
            return self._error(str(exc), path=self.path)
