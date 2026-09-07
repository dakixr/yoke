from __future__ import annotations

# ruff: noqa: D100,D102,E501,S603

import json
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import ConfigDict, Field, model_validator

from yoke.agent.tools.base import WorkspaceTool
from yoke.agent.tools.search_common import bound_text
from yoke.agent.tools.search_common import parse_nul_counts
from yoke.agent.tools.search_common import parse_rg_json_items
from yoke.agent.tools.search_common import split_nul_paths


RgMode = Literal["matches", "files", "files_with_matches", "count"]
RgCase = Literal["sensitive", "insensitive", "smart"]
RgMatching = Literal["regex", "fixed", "word"]
RgIgnore = Literal["normal", "none", "no_vcs"]
RgTake = Literal["first", "last"]
RgSort = Literal["none", "path"]


def _resolve_rg_binary() -> str:
    rg_path = shutil.which("rg")
    if rg_path:
        return rg_path
    raise FileNotFoundError("ripgrep binary 'rg' was not found on PATH")


class RipgrepTool(WorkspaceTool):
    """Search file contents or list files using typed ripgrep semantics."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    is_yoke_tool = True
    read_rg_config: ClassVar[bool] = True
    name = "rg"
    description = (
        "Search file contents or list files using typed ripgrep options. Do not "
        "write shell pipelines here. Use limit/sort for bounded results and "
        "exec_command for shell composition or command execution."
    )

    patterns: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Regex or literal search patterns. Required except in mode='files'. "
            "Multiple patterns are ORed, matching repeated rg -e arguments."
        ),
    )
    paths: list[str] = Field(
        default_factory=list,
        max_length=32,
        description=(
            "Files or directories to search. Relative paths resolve from root_dir. "
            "When empty, search root_dir itself."
        ),
    )
    root_dir: str | None = Field(
        default=None,
        description=(
            "Directory in which ripgrep runs. It must be a directory. Use paths "
            "for individual files. Relative values resolve from the workspace root."
        ),
    )
    mode: RgMode = Field(
        default="matches",
        description=(
            "Result mode: structured matches, all files, files containing a match, "
            "or per-file match counts."
        ),
    )
    globs: list[str] = Field(
        default_factory=list,
        max_length=32,
        description="Include or exclude globs, including negative globs such as !vendor/**.",
    )
    types: list[str] = Field(
        default_factory=list,
        max_length=16,
        description="Ripgrep file types such as py, js, rust, or md.",
    )
    case: RgCase = Field(default="sensitive")
    matching: RgMatching = Field(
        default="regex",
        description="Regex, fixed-string, or whole-word matching.",
    )
    pcre2: bool = Field(default=False)
    multiline: bool = Field(default=False)
    invert: bool = Field(default=False)
    context_before: int = Field(default=0, ge=0, le=500)
    context_after: int = Field(default=0, ge=0, le=500)
    max_matches_per_file: int | None = Field(default=None, ge=1, le=100_000)
    only_matching: bool = Field(
        default=False,
        description=(
            "Return one structured item per submatch instead of the containing line."
        ),
    )
    hidden: bool = Field(default=False)
    ignore: RgIgnore = Field(
        default="normal",
        description="normal respects ignore files, none ignores all ignore rules, no_vcs ignores VCS rules only.",
    )
    sort: RgSort = Field(default="none")
    limit: int | None = Field(
        default=None,
        ge=1,
        le=10_000,
        description="Global number of structured result items to return, replacing | head.",
    )
    take: RgTake = Field(
        default="first",
        description="Take the first or last limit results. 'last' requires limit.",
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)

    @model_validator(mode="after")
    def validate_semantics(self) -> RipgrepTool:
        if self.mode == "files":
            if self.patterns:
                raise ValueError("patterns must be empty when mode='files'")
        elif not self.patterns:
            raise ValueError("patterns must contain at least one pattern")
        if self.only_matching and self.mode != "matches":
            raise ValueError("only_matching is valid only when mode='matches'")
        if self.take == "last" and self.limit is None:
            raise ValueError("take='last' requires limit")
        if self.pcre2 and self.matching != "regex":
            raise ValueError("pcre2 requires matching='regex'")
        return self

    def execute(self) -> dict[str, object]:
        try:
            rg_binary = _resolve_rg_binary()
            search_root = self._resolve_search_root()
            command = self._build_command(rg_binary, search_root)
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
            return self._error("rg timed out after 20 seconds")
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
            raise ValueError(f"Invalid rg root_dir: {exc}") from exc
        if not root_dir.is_dir():
            raise ValueError(
                f"rg root_dir is not a directory: {root_dir}. Use paths for files."
            )
        return root_dir

    def _build_command(self, binary: str, search_root: Path) -> list[str]:
        command = [binary]
        if not self.read_rg_config:
            command.append("--no-config")

        if self.mode == "matches":
            command.append("--json")
        elif self.mode == "files":
            command.extend(["--files", "--null"])
        elif self.mode == "files_with_matches":
            command.extend(["--files-with-matches", "--null"])
        elif self.mode == "count":
            command.extend(["--count", "--with-filename", "--null"])

        if self.case == "insensitive":
            command.append("--ignore-case")
        elif self.case == "smart":
            command.append("--smart-case")

        if self.matching == "fixed":
            command.append("--fixed-strings")
        elif self.matching == "word":
            command.append("--word-regexp")
        if self.pcre2:
            command.append("--pcre2")
        if self.multiline:
            command.append("--multiline")
        if self.invert:
            command.append("--invert-match")
        if self.context_before:
            command.extend(["--before-context", str(self.context_before)])
        if self.context_after:
            command.extend(["--after-context", str(self.context_after)])
        if self.max_matches_per_file is not None:
            command.extend(["--max-count", str(self.max_matches_per_file)])
        if self.hidden:
            command.append("--hidden")
        if self.ignore == "none":
            command.append("--no-ignore")
        elif self.ignore == "no_vcs":
            command.append("--no-ignore-vcs")
        if self.sort == "path":
            command.extend(["--sort", "path"])
        for glob in self.globs:
            command.extend(["--glob", glob])
        for file_type in self.types:
            command.extend(["--type", file_type])
        for pattern in self.patterns:
            command.extend(["--regexp", pattern])

        command.append("--")
        command.extend(self.paths or [str(search_root)])
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
                self._combined_error(stdout, stderr, exit_code), self.max_output_chars
            )
            result = self._error(
                error,
                command=command,
                exit_code=exit_code,
            )
            if truncated:
                result["truncated"] = True
            return result

        items: list[object]
        if self.mode == "matches":
            items = list(parse_rg_json_items(stdout, only_matching=self.only_matching))
            label = "matches"
        elif self.mode in {"files", "files_with_matches"}:
            items = list(self._parse_null_paths(stdout))
            label = "paths"
        else:
            items = list(self._parse_counts(stdout))
            label = "counts"

        items = self._take_items(items)
        result = self._bounded_result(items, command, exit_code, label)
        if stderr.strip():
            diagnostic, diagnostic_truncated = bound_text(
                stderr.rstrip("\r\n"), self.max_output_chars
            )
            result["stderr"] = diagnostic
            if diagnostic_truncated:
                result["truncated"] = True
        return result

    @staticmethod
    def _parse_null_paths(stdout: str) -> list[str]:
        return split_nul_paths(stdout)

    @staticmethod
    def _parse_counts(stdout: str) -> list[dict[str, object]]:
        return parse_nul_counts(stdout)

    def _take_items(self, items: list[object]) -> list[object]:
        if self.limit is None:
            return items
        if self.take == "last":
            return items[-self.limit :]
        return items[: self.limit]

    def _bounded_result(
        self,
        items: list[object],
        command: list[str],
        exit_code: int,
        label: str,
    ) -> dict[str, object]:
        included: list[object] = []
        truncated = False
        for item in items:
            candidate = self._success(
                command=command, output=[*included, item], exit_code=exit_code
            )
            if len(json.dumps(candidate, ensure_ascii=False)) > self.max_output_chars:
                truncated = True
                break
            included.append(item)
        result = self._success(command=command, output=included, exit_code=exit_code)
        if truncated or len(included) < len(items):
            result["truncated"] = True
            result["summary"] = f"showing {len(included)} {label}"
        return result

    @staticmethod
    def _combined_error(stdout: str, stderr: str, exit_code: int) -> str:
        parts = [part.rstrip("\r\n") for part in (stderr, stdout) if part.strip()]
        return "\n".join(parts) or f"rg failed with exit code {exit_code}"
