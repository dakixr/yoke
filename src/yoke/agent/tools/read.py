"""Tool for reading text files from the workspace."""

from __future__ import annotations

from pydantic import Field

from yoke.agent.truncate import DEFAULT_MAX_BYTES
from yoke.agent.truncate import format_size
from yoke.agent.truncate import truncate_head

from .base import WorkspaceTool


def single_line_read_hint(path: str, line_number: int) -> str:
    """Return a platform hint for reading a single oversized line."""
    escaped_path = path.replace("'", "'\"'\"'")
    return (
        f"Use zsh: sed -n '{line_number}p' '{escaped_path}'"
        f" | head -c {DEFAULT_MAX_BYTES}"
    )


DEFAULT_READ_LIMIT = 150


class ReadTool(WorkspaceTool):
    """Tool that reads a text file from the workspace."""

    name = "read"
    description = (
        "Read a UTF-8 text file. Relative paths resolve from the "
        "configured root. Defaults to the first 150 lines when limit "
        "is omitted. Use offset/limit to continue. "
        "Output is truncated to 2000 lines or 50KB."
    )

    path: str = Field(min_length=1)
    offset: int | None = Field(default=None, ge=1)
    limit: int | None = Field(default=None, ge=1)

    def execute(self) -> dict[str, object]:
        """Read the file and return its content with metadata."""
        try:
            path = self._resolve_path(self.path)
            self._ensure_text_file(path)
            content = path.read_text(encoding="utf-8")
            all_lines = content.split("\n")
            start = (self.offset or 1) - 1
            if start >= len(all_lines):
                raise ValueError(
                    f"Offset {self.offset} is beyond end of file "
                    f"({len(all_lines)} lines total)"
                )
            effective_limit = self.limit or DEFAULT_READ_LIMIT
            selected_lines = all_lines[start : start + effective_limit]
            selected_content = "\n".join(selected_lines)
            truncation = truncate_head(selected_content)
            start_line = start + 1
            next_offset = None
            output_text = truncation.content
            details: dict[str, object] | None = None

            if truncation.first_line_exceeds_limit:
                first_line_size = format_size(len(all_lines[start].encode("utf-8")))
                size_limit = format_size(DEFAULT_MAX_BYTES)
                hint = single_line_read_hint(self.path, start_line)
                output_text = (
                    f"[Line {start_line} is {first_line_size}, "
                    f"exceeds {size_limit} limit. {hint}]"
                )
                details = {"truncation": truncation.to_dict()}
            elif truncation.truncated:
                end_line = start_line + truncation.output_lines - 1
                next_offset = end_line + 1
                total = len(all_lines)
                if truncation.truncated_by == "lines":
                    suffix = (
                        f"[Showing lines {start_line}-{end_line} of "
                        f"{total}. Use offset={next_offset} to continue.]"
                    )
                else:
                    size_limit = format_size(DEFAULT_MAX_BYTES)
                    suffix = (
                        f"[Showing lines {start_line}-{end_line} of "
                        f"{total} ({size_limit} limit). "
                        f"Use offset={next_offset} to continue.]"
                    )
                output_text = f"{truncation.content}\n\n{suffix}"
                details = {"truncation": truncation.to_dict()}
            elif start + effective_limit < len(all_lines):
                next_offset = start + effective_limit + 1
                remaining = len(all_lines) - (start + effective_limit)
                output_text = (
                    f"{truncation.content}\n\n"
                    f"[{remaining} more lines in file. "
                    f"Use offset={next_offset} to continue.]"
                )

            result = self._success(
                path=self.path,
                content=output_text,
                offset=self.offset or 1,
                limit=effective_limit,
            )
            if next_offset is not None:
                result["next_offset"] = next_offset
            if details is not None:
                result["details"] = details
            return result
        except Exception as exc:
            return self._error(str(exc), path=self.path)
