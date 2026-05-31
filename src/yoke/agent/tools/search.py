"""Tools for listing files, finding paths, and grepping content."""

from __future__ import annotations

import re
from fnmatch import fnmatch

from pydantic import Field

from yoke.agent.truncate import truncate_line

from .base import DEFAULT_GLOB
from .base import WorkspaceTool


class LsTool(WorkspaceTool):
    """Tool that lists files and directories under a given path."""

    name = "ls"
    description = (
        "List files and directories under a path. Relative paths "
        "resolve from the configured root."
    )

    path: str = Field(default=".", min_length=1)
    recursive: bool = False
    limit: int = Field(default=200, ge=1, le=5_000)

    def execute(self) -> dict[str, object]:
        """List directory entries and return them."""
        try:
            path = self._resolve_path(self.path)
            if path.is_file():
                entries = [self._display_path(path)]
            else:
                iterator = (
                    self._walk(path) if self.recursive else sorted(path.iterdir())
                )
                entries = [
                    self._display_path(entry) for entry in iterator if entry != path
                ][: self.limit]
            result = self._success(entries=entries)
            if self.recursive:
                result["recursive"] = True
            if len(entries) >= self.limit:
                result["truncated"] = True
            return result
        except Exception as exc:
            return self._error(str(exc), path=self.path)


class FindTool(WorkspaceTool):
    """Tool that finds files or directories by glob pattern."""

    name = "find"
    description = "Find files or directories whose displayed path matches a glob."

    path: str = Field(default=".", min_length=1)
    pattern: str = Field(min_length=1)
    limit: int = Field(default=100, ge=1, le=5_000)

    def execute(self) -> dict[str, object]:
        """Find matching files and return their paths."""
        try:
            path = self._resolve_path(self.path)
            matches: list[str] = []
            for candidate in self._walk(path):
                relative = self._display_path(candidate)
                if fnmatch(relative, self.pattern) or fnmatch(
                    candidate.name, self.pattern
                ):
                    matches.append(relative)
                if len(matches) >= self.limit:
                    break
            result = self._success()
            if matches:
                result["matches"] = matches
            if len(matches) >= self.limit:
                result["truncated"] = True
            return result
        except Exception as exc:
            return self._error(str(exc), path=self.path, pattern=self.pattern)


class GrepTool(WorkspaceTool):
    """Tool that searches text files using a regular expression."""

    name = "grep"
    description = (
        "Search text files with a regular expression. Relative paths "
        "resolve from the configured root."
    )

    path: str = Field(default=".", min_length=1)
    pattern: str = Field(min_length=1)
    glob: str = Field(default=DEFAULT_GLOB, min_length=1)
    limit: int = Field(default=50, ge=1, le=5_000)

    def execute(self) -> dict[str, object]:
        """Search files for the regex pattern and return matches."""
        try:
            root = self._resolve_path(self.path)
            regex = re.compile(self.pattern)
            files: list[dict[str, object]] = []
            match_count = 0
            truncated = False
            for candidate in self._iter_files(root, glob=self.glob):
                try:
                    text = candidate.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                file_matches: list[dict[str, object]] = []
                for line_number, line in enumerate(text.splitlines(), start=1):
                    if not regex.search(line):
                        continue
                    rendered_line, line_truncated = truncate_line(line)
                    match_payload: dict[str, object] = {
                        "line": line_number,
                        "text": rendered_line,
                    }
                    if line_truncated:
                        match_payload["line_truncated"] = True
                    file_matches.append(match_payload)
                    match_count += 1
                    if match_count >= self.limit:
                        truncated = True
                        break
                if file_matches:
                    files.append(
                        {
                            "path": self._display_path(candidate),
                            "matches": file_matches,
                        }
                    )
                if truncated:
                    break
            result = self._success(match_count=match_count)
            if files:
                result["files"] = files
            if truncated:
                result["truncated"] = True
            return result
        except Exception as exc:
            return self._error(
                str(exc), path=self.path, pattern=self.pattern, glob=self.glob
            )
