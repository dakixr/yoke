"""First-class typed fd file-discovery tool."""

from __future__ import annotations

# ruff: noqa: S603

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from yoke.agent.tools.base import WorkspaceTool
from yoke.agent.tools.search_common import bound_text
from yoke.agent.tools.search_common import path_detail
from yoke.agent.tools.search_common import split_nul_paths


FdType = Literal[
    "file",
    "directory",
    "symlink",
    "executable",
    "empty",
    "socket",
    "pipe",
]
FdMatch = Literal["regex", "glob", "fixed"]
FdCase = Literal["auto", "sensitive", "insensitive"]
FdIgnore = Literal["normal", "none", "no_vcs"]
FdTake = Literal["first", "last"]
FdSort = Literal["none", "path"]


def _resolve_fd_binary() -> str:
    fd_path = shutil.which("fd")
    if fd_path:
        return fd_path
    raise FileNotFoundError("fd binary was not found on PATH")


class FdTool(WorkspaceTool):
    """Find files and directories using typed fd search semantics."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    is_yoke_tool = True
    name = "fd"
    provider_result_projection = "fd"
    description = (
        "Find files and directories using typed fd options. Do not write shell "
        "pipelines or fd execution flags here. Use limit/sort/filter_pattern for "
        "result shaping and exec_command when command execution is intended."
    )

    pattern: str | None = Field(
        default=None,
        description="Name/path pattern. Empty means match every entry.",
    )
    paths: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Directories to search beneath root_dir. Multiple paths are supported. "
            "Relative paths resolve from root_dir."
        ),
    )
    root_dir: str | None = Field(
        default=None,
        description=(
            "Directory in which fd runs. It must be a directory. Use paths for "
            "search locations beneath it. Relative values resolve from the workspace root."
        ),
    )
    types: list[FdType] = Field(default_factory=list, max_length=8)
    extensions: list[str] = Field(default_factory=list, max_length=32)
    excludes: list[str] = Field(default_factory=list, max_length=32)
    match_mode: FdMatch = Field(default="regex")
    case: FdCase = Field(default="auto")
    full_path: bool = Field(
        default=False,
        description="Match the pattern against the full path instead of only the basename.",
    )
    hidden: bool = Field(default=False)
    ignore: FdIgnore = Field(default="normal")
    ignore_files: list[str] = Field(default_factory=list, max_length=8)
    follow_symlinks: bool = Field(default=False)
    min_depth: int | None = Field(default=None, ge=0, le=10_000)
    max_depth: int | None = Field(default=None, ge=0, le=10_000)
    changed_within: str | None = Field(default=None)
    changed_before: str | None = Field(default=None)
    size: str | None = Field(default=None)
    absolute_paths: bool = Field(default=False)
    details: bool = Field(
        default=False,
        description="Return structured path metadata instead of plain path strings.",
    )
    filter_pattern: str | None = Field(
        default=None,
        description=(
            "Optional regex applied to returned paths/details, replacing simple | rg or | grep filters."
        ),
    )
    sort: FdSort = Field(default="none")
    limit: int | None = Field(
        default=None,
        ge=1,
        le=10_000,
        description="Global result count limit, replacing | head.",
    )
    take: FdTake = Field(default="first")
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)

    @model_validator(mode="after")
    def validate_semantics(self) -> FdTool:
        if self.take == "last" and self.limit is None:
            raise ValueError("take='last' requires limit")
        if (
            self.min_depth is not None
            and self.max_depth is not None
            and self.min_depth > self.max_depth
        ):
            raise ValueError("min_depth cannot exceed max_depth")
        if self.filter_pattern is not None:
            try:
                re.compile(self.filter_pattern)
            except re.error as exc:
                raise ValueError(f"Invalid filter_pattern regex: {exc}") from exc
        return self

    def execute(self) -> dict[str, object]:
        try:
            fd_binary = _resolve_fd_binary()
            search_root = self._resolve_search_root()
            command = self._build_command(fd_binary, search_root)
        except (FileNotFoundError, ValueError) as exc:
            return self._error(str(exc))

        try:
            completed = subprocess.run(
                command,
                cwd=search_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._error("fd timed out after 20 seconds")
        except Exception as exc:
            return self._error(str(exc))

        return self._render_output(
            completed.stdout,
            completed.stderr,
            command,
            completed.returncode,
        )

    def _resolve_search_root(self) -> Path:
        if self.root_dir is None:
            return self.root
        try:
            root_dir = self._resolve_path(self.root_dir)
        except Exception as exc:
            raise ValueError(f"Invalid fd root_dir: {exc}") from exc
        if not root_dir.is_dir():
            raise ValueError(
                f"fd root_dir is not a directory: {root_dir}. Use paths for search locations."
            )
        return root_dir

    def _build_command(self, binary: str, search_root: Path) -> list[str]:
        del search_root
        command = [binary]
        for file_type in self.types:
            command.extend(["--type", file_type])
        for extension in self.extensions:
            command.extend(["--extension", extension])
        for exclude in self.excludes:
            command.extend(["--exclude", exclude])
        if self.match_mode == "glob":
            command.append("--glob")
        elif self.match_mode == "fixed":
            command.append("--fixed-strings")
        if self.case == "sensitive":
            command.append("--case-sensitive")
        elif self.case == "insensitive":
            command.append("--ignore-case")
        if self.full_path:
            command.append("--full-path")
        if self.hidden:
            command.append("--hidden")
        if self.ignore == "none":
            command.append("--no-ignore")
        elif self.ignore == "no_vcs":
            command.append("--no-ignore-vcs")
        for ignore_file in self.ignore_files:
            command.extend(["--ignore-file", ignore_file])
        if self.follow_symlinks:
            command.append("--follow")
        if self.min_depth is not None:
            command.extend(["--min-depth", str(self.min_depth)])
        if self.max_depth is not None:
            command.extend(["--max-depth", str(self.max_depth)])
        if self.changed_within is not None:
            command.extend(["--changed-within", self.changed_within])
        if self.changed_before is not None:
            command.extend(["--changed-before", self.changed_before])
        if self.size is not None:
            command.extend(["--size", self.size])
        if self.absolute_paths:
            command.append("--absolute-path")
        command.append("--print0")

        can_limit_natively = (
            self.limit is not None
            and self.take == "first"
            and self.sort == "none"
            and self.filter_pattern is None
        )
        if can_limit_natively:
            command.extend(["--max-results", str(self.limit)])

        for path in self.paths:
            command.extend(["--search-path", path])
        if self.pattern is not None:
            command.extend(["--", self.pattern])
        return command

    def _render_output(
        self,
        stdout: str,
        stderr: str,
        command: list[str],
        exit_code: int,
    ) -> dict[str, object]:
        if exit_code not in {0, 1}:
            error, truncated = bound_text(
                self._combined_error(stdout, stderr), self.max_output_chars
            )
            result = self._error(
                error,
                command=command,
                exit_code=exit_code,
            )
            if truncated:
                result["truncated"] = True
            return result

        items = split_nul_paths(stdout)

        if self.filter_pattern is not None:
            regex = re.compile(self.filter_pattern)
            items = [item for item in items if regex.search(item)]
        if self.sort == "path":
            items.sort()
        if self.limit is not None:
            if self.take == "last":
                items = items[-self.limit :]
            else:
                items = items[: self.limit]

        shaped: list[object]
        if self.details:
            search_root = self._resolve_search_root()
            shaped = [path_detail(item, search_root) for item in items]
        else:
            shaped = list(items)

        output: list[object] = []
        truncated = False
        for item in shaped:
            candidate = self._success(
                command=command,
                output=[*output, item],
                exit_code=exit_code,
            )
            if len(json.dumps(candidate, ensure_ascii=False)) > self.max_output_chars:
                truncated = True
                break
            output.append(item)

        result = self._success(command=command, output=output, exit_code=exit_code)
        if stderr.strip():
            diagnostic, diagnostic_truncated = bound_text(
                stderr.rstrip("\r\n"), self.max_output_chars
            )
            result["stderr"] = diagnostic
            if diagnostic_truncated:
                result["truncated"] = True
        if truncated or len(output) < len(shaped):
            result["truncated"] = True
            result["summary"] = f"showing {len(output)} results"
        return result

    @staticmethod
    def _combined_error(stdout: str, stderr: str) -> str:
        parts = [part.rstrip("\r\n") for part in (stderr, stdout) if part.strip()]
        return "\n".join(parts) or "fd failed"
