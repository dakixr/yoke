"""Python interpreter environment helpers for local tools."""

from __future__ import annotations

import os
import shlex
import stat
import sys
import tempfile
from pathlib import Path


PYTHON_BIN_DIR_ENV = "YOKE_PYTHON_BIN_DIR"
PYTHON_EXECUTABLE_ENV = "YOKE_PYTHON_EXECUTABLE"


def current_python_executable() -> str:
    """Return the interpreter used by the running yoke process."""
    executable = Path(sys.executable)
    if executable.is_absolute():
        return str(executable)
    return str(executable.absolute())


def prepare_python_env(env: dict[str, str]) -> dict[str, str]:
    """Expose stable `python` and `python3` commands for child processes."""
    python_executable = current_python_executable()
    bin_dir = ensure_python_alias_bin(python_executable)
    path = env.get("PATH", "")
    env[PYTHON_EXECUTABLE_ENV] = python_executable
    env[PYTHON_BIN_DIR_ENV] = str(bin_dir)
    env["PATH"] = f"{bin_dir}{os.pathsep}{path}" if path else str(bin_dir)
    return env


def ensure_python_alias_bin(python_executable: str | None = None) -> Path:
    """Create a temp bin dir with `python`/`python3` shims to yoke's Python."""
    executable = python_executable or current_python_executable()
    if os.name == "nt":
        return ensure_windows_python_alias_bin(executable)

    bin_dir = Path(tempfile.gettempdir()) / f"yoke-python-bin-{os.getuid()}"
    bin_dir.mkdir(mode=0o700, exist_ok=True)
    for name in ("python", "python3"):
        shim = bin_dir / name
        desired = _python_shim(executable)
        if not _same_file_content(shim, desired):
            shim.write_text(desired, encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    return bin_dir


def ensure_windows_python_alias_bin(python_executable: str) -> Path:
    """Create a temp bin dir with Windows command shims to yoke's Python."""
    bin_dir = Path(tempfile.gettempdir()) / "yoke-python-bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("python", "python3"):
        shim = bin_dir / f"{name}.cmd"
        desired = _python_cmd_shim(python_executable)
        if not _same_file_content(shim, desired):
            shim.write_text(desired, encoding="utf-8")
    return bin_dir


def _python_shim(executable: str) -> str:
    return f'#!/bin/sh\nexec {shlex.quote(executable)} "$@"\n'


def _python_cmd_shim(executable: str) -> str:
    return f'@echo off\r\n"{executable}" %*\r\nexit /b %ERRORLEVEL%\r\n'


def _same_file_content(path: Path, content: str) -> bool:
    try:
        return path.read_text(encoding="utf-8") == content
    except OSError:
        return False
