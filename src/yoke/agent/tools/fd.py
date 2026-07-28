"""First-class fd file-discovery tool."""

from __future__ import annotations

# ruff: noqa: S603

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from pydantic import Field

from yoke.agent.tools.base import WorkspaceTool


class FdTool(WorkspaceTool):
    """Run fd for fast file and directory discovery."""

    is_yoke_tool = True
    name = "fd"
    description = (
        "Run fd, the fast and user-friendly file finder. Pass raw_args exactly "
        "as you would after `fd`; supports regex/glob search, ignore files, "
        "hidden paths, extensions, types, depth, and excludes. Prefer fd for "
        "discovering files and directories by name or path."
    )

    raw_args: str = Field(
        default="",
        description="Exact arguments to pass after `fd`; empty lists all files.",
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
            binary = self._find_fd_binary()
            search_root = self._resolve_search_root()
            command = [binary, *self._parse_raw_args()]
        except (FileNotFoundError, ValueError) as exc:
            return {"ok": False, "output": str(exc)}

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
            return {"ok": False, "output": "fd timed out after 20 seconds"}
        except Exception as exc:
            return {"ok": False, "output": str(exc)}

        if completed.returncode not in {0, 1}:
            return {
                "ok": False,
                "command": command,
                "exit_code": completed.returncode,
                "output": self._combined_output(completed.stdout, completed.stderr),
            }
        return self._render_output(completed.stdout, completed.stderr, command)

    @staticmethod
    def _find_fd_binary() -> str:
        binary = shutil.which("fd")
        if binary:
            return binary
        raise FileNotFoundError("fd binary 'fd' was not found on PATH")

    def _resolve_search_root(self) -> Path:
        if self.root_dir is None:
            return self.root
        try:
            root = self._resolve_path(self.root_dir)
        except Exception as exc:
            raise ValueError(f"Invalid fd root_dir: {exc}") from exc
        if not root.is_dir():
            raise ValueError(f"fd root_dir is not a directory: {root}")
        return root

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
    ) -> dict[str, object]:
        separator = "\0" if "\0" in stdout else None
        paths = stdout.rstrip("\0\r\n").split(separator) if stdout else []
        output: list[str] = []
        output_chars = 0
        for path in paths:
            path = path.rstrip("\r")
            if output_chars + len(path) + 1 > self.max_output_chars:
                return {
                    "ok": True,
                    "command": command,
                    "output": output,
                    "exit_code": 0,
                    "truncated": True,
                    "summary": f"showing {len(output)} paths",
                }
            output.append(path)
            output_chars += len(path) + 1
        result: dict[str, object] = {
            "ok": True,
            "command": command,
            "output": output,
            "exit_code": 0,
        }
        if stderr.strip():
            result["stderr"] = stderr.rstrip("\r\n")
        return result

    @staticmethod
    def _combined_output(stdout: str, stderr: str) -> str:
        parts = [part.rstrip("\r\n") for part in (stderr, stdout) if part.strip()]
        return "\n".join(parts) or "fd failed"
