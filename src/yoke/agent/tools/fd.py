"""First-class fd file-discovery tool."""

from __future__ import annotations

# ruff: noqa: S603

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from yoke.agent.tools.base import WorkspaceTool
from pydantic import Field


def _resolve_fd_binary() -> str:
    fd_path = shutil.which("fd")
    if fd_path:
        return fd_path
    raise FileNotFoundError("fd binary was not found on PATH")


class FdTool(WorkspaceTool):
    """Run fd for fast, ergonomic file and directory discovery."""

    is_yoke_tool = True
    name = "fd"
    description = (
        "Run fd, the fast and user-friendly file finder. Pass raw_args exactly "
        "as you would after `fd`; supports regex/glob search, ignore files, "
        "hidden paths, extensions, types, depth, excludes, and execution. "
        "Prefer fd for discovering files and directories by name or path."
    )

    raw_args: str = Field(
        default="",
        description=("Exact arguments to pass after `fd`; empty lists all files."),
    )
    root_dir: str | None = Field(
        default=None,
        description=(
            "Optional directory in which fd runs. Relative paths resolve from "
            "the workspace root."
        ),
    )
    max_output_chars: int = Field(default=12_000, ge=1, le=200_000)

    def execute(self) -> dict[str, object]:
        """Run fd and return bounded path results."""
        try:
            fd_binary = _resolve_fd_binary()
            search_root = self._resolve_search_root()
            command = [fd_binary, *self._parse_raw_args()]
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

        if completed.returncode not in {0, 1}:
            return self._error(
                self._combined_error(completed.stdout, completed.stderr),
                command=command,
                exit_code=completed.returncode,
            )
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
            raise ValueError(f"fd root_dir is not a directory: {root_dir}")
        return root_dir

    def _parse_raw_args(self) -> list[str]:
        argv = shlex.split(self.raw_args, posix=os.name != "nt")
        if os.name != "nt":
            return argv
        return [self._strip_wrapping_quotes(arg) for arg in argv]

    @staticmethod
    def _strip_wrapping_quotes(value: str) -> str:
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            return value[1:-1]
        return value

    def _render_output(
        self,
        stdout: str,
        stderr: str,
        command: list[str],
        exit_code: int,
    ) -> dict[str, object]:
        separator = "\0" if "\0" in stdout else None
        paths = stdout.rstrip("\0\r\n").split(separator) if stdout else []
        output: list[str] = []
        output_chars = 0
        truncated = False
        for path in paths:
            path = path.rstrip("\r")
            added_chars = len(path) + 1
            if output_chars + added_chars > self.max_output_chars:
                truncated = True
                break
            output.append(path)
            output_chars += added_chars
        result = self._success(
            command=command,
            output=output,
            exit_code=exit_code,
        )
        if stderr.strip():
            result["stderr"] = stderr.rstrip("\r\n")
        if truncated:
            result["truncated"] = True
            result["summary"] = f"showing {len(output)} paths"
        return result

    @staticmethod
    def _combined_error(stdout: str, stderr: str) -> str:
        parts = [part.rstrip("\r\n") for part in (stderr, stdout) if part.strip()]
        return "\n".join(parts) or "fd failed"
