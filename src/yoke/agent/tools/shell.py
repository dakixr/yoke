"""Shell command building utilities for platform-appropriate execution."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from pathlib import PureWindowsPath

COMMAND_TOOL_NAME = "exec_command"


def default_shell_executable(env: dict[str, str]) -> str:
    """Return the appropriate shell executable path for the current platform."""
    if os.name == "nt":
        if shell_override := env.get("YOKE_SHELL"):
            return shell_override
        if pwsh := shutil.which("pwsh.exe") or shutil.which("pwsh"):
            return pwsh
        if powershell := shutil.which("powershell.exe") or shutil.which("powershell"):
            return powershell
        return env.get("ComSpec") or "cmd.exe"

    for candidate in (env.get("YOKE_ZSH"), env.get("SHELL")):
        if candidate and Path(candidate).name.lower() == "zsh":
            return candidate
    if zsh := shutil.which("zsh"):
        return zsh
    return "/bin/zsh"


def build_shell_command(
    command: str,
    env: dict[str, str],
    *,
    shell: str | None = None,
    login: bool = True,
) -> list[str]:
    """Build a platform-appropriate shell command list for subprocess."""
    shell_exe = shell or default_shell_executable(env)
    if os.name == "nt":
        shell_name = _shell_name(shell_exe)
        if shell_name in {"powershell.exe", "powershell", "pwsh.exe", "pwsh"}:
            return build_powershell_command(command, env, shell_exe, shell_name)
        if shell_name in {"cmd.exe", "cmd"}:
            return [shell_exe, "/d", "/s", "/c", command]
        return [shell_exe, "-lc" if login else "-c", command]

    env["YOKE_COMMAND_TOOL_COMMAND"] = command
    flags = ["-l", "-c"] if login else ["-c"]
    return [shell_exe, *flags, _zsh_command(load_profile=login)]


def build_powershell_command(
    command: str,
    env: dict[str, str],
    shell_exe: str,
    shell_name: str,
) -> list[str]:
    """Build a PowerShell command list for the given command and shell."""
    if shell_name in {"powershell.exe", "powershell"}:
        command = rewrite_powershell_command(command)
    env["YOKE_COMMAND_TOOL_COMMAND"] = command
    has_active_python_env = bool(env.get("VIRTUAL_ENV") or env.get("CONDA_PREFIX"))
    profile_loader = (
        ""
        if has_active_python_env
        else "if (Test-Path -LiteralPath $PROFILE) { . $PROFILE }; "
    )
    ps_command = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        "$OutputEncoding = [System.Text.Encoding]::UTF8; "
        f"{profile_loader}"
        "if ($env:YOKE_PYTHON_BIN_DIR) { "
        '$env:Path = "$env:YOKE_PYTHON_BIN_DIR;$env:Path" }; '
        "Invoke-Expression $env:YOKE_COMMAND_TOOL_COMMAND"
    )
    return [
        shell_exe,
        "-NoLogo",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        ps_command,
    ]


def rewrite_powershell_command(command: str) -> str:
    """Rewrite a command string to be compatible with Windows PowerShell syntax."""
    command = rewrite_legacy_powershell_chain_operators(command)
    stripped = command.lstrip()
    if stripped.startswith("& "):
        return command
    quoted_command = re.match(
        r'^(?P<indent>\s*)(?P<quoted>(?P<quote>["\']).+?(?P=quote))(?=\s)',
        command,
    )
    if quoted_command is not None:
        indent = quoted_command.group("indent")
        quoted = quoted_command.group("quoted")
        remainder = command[quoted_command.end() :]
        return f"{indent}& {quoted}{remainder}"
    return command


def rewrite_legacy_powershell_chain_operators(command: str) -> str:
    """Replace bash-style && operators with PowerShell-compatible semicolons."""
    if "&&" not in command:
        return command
    return command.replace("&&", ";")


def _shell_name(shell_exe: str) -> str:
    if os.name == "nt":
        return PureWindowsPath(shell_exe).name.lower()
    return Path(shell_exe).name.lower()


def _zsh_command(
    env_var: str = "YOKE_PYTHON_BIN_DIR",
    *,
    load_profile: bool,
) -> str:
    profile = (
        "[[ -f ~/.zshenv ]] && source ~/.zshenv >/dev/null 2>&1 || true; "
        '[[ -f "${ZDOTDIR:-$HOME}/.zshrc" ]] '
        '&& source "${ZDOTDIR:-$HOME}/.zshrc" >/dev/null 2>&1 || true; '
        if load_profile
        else ""
    )
    return profile + (
        f'[[ -n "${{{env_var}:-}}" ]] && export PATH="${{{env_var}}}:$PATH"; '
        'eval "$YOKE_COMMAND_TOOL_COMMAND"'
    )
