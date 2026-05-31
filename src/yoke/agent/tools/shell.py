"""Shell command building utilities for zsh execution."""

from __future__ import annotations

from pathlib import Path
import shutil

COMMAND_TOOL_NAME = "bash"


def default_shell_executable(env: dict[str, str]) -> str:
    """Return a zsh executable."""
    for candidate in (env.get("YOKE_ZSH"), env.get("SHELL")):
        if candidate and Path(candidate).name.lower() == "zsh":
            return candidate
    if zsh := shutil.which("zsh"):
        return zsh
    return "/bin/zsh"


def build_shell_command(command: str, env: dict[str, str]) -> list[str]:
    """Build a zsh command list for subprocess."""
    shell_exe = default_shell_executable(env)
    return [shell_exe, "-l", "-c", _zsh_login_command(command)]


def _zsh_login_command(command: str, env_var: str = "YOKE_PYTHON_BIN_DIR") -> str:
    return (
        "[[ -f ~/.zshenv ]] && source ~/.zshenv >/dev/null 2>&1 || true; "
        '[[ -f "${ZDOTDIR:-$HOME}/.zshrc" ]] '
        '&& source "${ZDOTDIR:-$HOME}/.zshrc" >/dev/null 2>&1 || true; '
        f'[[ -n "${{{env_var}:-}}" ]] && export PATH="${{{env_var}}}:$PATH"; '
        "source /dev/stdin"
    )
