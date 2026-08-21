"""Stable MCP facades over Yoke's search and discovery tools."""

from __future__ import annotations

import shlex
import shutil

from pydantic import Field

from yoke.agent.tools.base import DEFAULT_GLOB
from yoke.agent.tools.base import WorkspaceTool
from yoke.agent.tools.fd import FdTool
from yoke.agent.tools.rg import RipgrepTool
from yoke.agent.tools.search import FindTool
from yoke.agent.tools.search import GrepTool


class SearchTextTool(WorkspaceTool):
    """Search text with ripgrep when available and a portable fallback."""

    name = "search_text"
    description = (
        "Search text in server files. Relative paths resolve from the configured "
        "default root. Use this to locate symbols, configuration values, error "
        "strings, and references before editing."
    )

    query: str = Field(min_length=1)
    path: str = Field(default=".", min_length=1)
    glob: str = Field(default=DEFAULT_GLOB, min_length=1)
    limit: int = Field(default=50, ge=1, le=5_000)

    def execute(self) -> dict[str, object]:
        """Run the selected Yoke search implementation."""
        if shutil.which("rg") is None:
            return self._portable_search()
        arguments = shlex.join(["--glob", self.glob, "--", self.query, self.path])
        tool = RipgrepTool.bind(root=self.root).parse_arguments(
            {"raw_args": arguments, "max_output_chars": 200_000}
        )
        result = tool.execute()
        output = result.get("output")
        if not result.get("ok", False) or not isinstance(output, list):
            return result
        matches = output[: self.limit]
        response = self._success(matches=matches, match_count=len(matches))
        if len(output) > self.limit or result.get("truncated"):
            response["truncated"] = True
        return response

    def _portable_search(self) -> dict[str, object]:
        tool = GrepTool.bind(root=self.root).parse_arguments(
            {
                "pattern": self.query,
                "path": self.path,
                "glob": self.glob,
                "limit": self.limit,
            }
        )
        return tool.execute()


class FindFilesTool(WorkspaceTool):
    """Find paths with fd when available and a portable fallback."""

    name = "find_files"
    description = (
        "Find server files or directories by glob pattern. Relative paths "
        "resolve from the configured default root."
    )

    pattern: str = Field(min_length=1)
    path: str = Field(default=".", min_length=1)
    limit: int = Field(default=100, ge=1, le=5_000)

    def execute(self) -> dict[str, object]:
        """Run the selected Yoke file discovery implementation."""
        if shutil.which("fd") is None:
            return self._portable_find()
        arguments = shlex.join(["--glob", self.pattern])
        tool = FdTool.bind(root=self.root).parse_arguments(
            {
                "raw_args": arguments,
                "root_dir": self.path,
                "max_output_chars": 200_000,
            }
        )
        result = tool.execute()
        output = result.get("output")
        if not result.get("ok", False) or not isinstance(output, list):
            return result
        matches = output[: self.limit]
        response = self._success(matches=matches)
        if len(output) > self.limit or result.get("truncated"):
            response["truncated"] = True
        return response

    def _portable_find(self) -> dict[str, object]:
        tool = FindTool.bind(root=self.root).parse_arguments(
            {"pattern": self.pattern, "path": self.path, "limit": self.limit}
        )
        return tool.execute()
