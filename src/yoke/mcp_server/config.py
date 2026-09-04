"""Configuration for the Yoke MCP server."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Validated runtime settings for one MCP application process."""

    root: Path
    host: str = "127.0.0.1"
    port: int = 8765
    default_yield_ms: int = 30_000
    python_timeout: int = 180
    max_output_tokens: int = 20_000
    max_concurrent_calls: int = 16
    max_concurrent_process_starts: int = 8
    max_request_body_size: int = 4 * 1024 * 1024
    allowed_hosts: tuple[str, ...] = ()
    skill_dirs: tuple[Path, ...] = ()
    bearer_token: str | None = None
    oauth_issuer_url: str | None = None
    oauth_authorization_password: str | None = None
    oauth_state_file: Path | None = None
    oauth_allowed_redirect_hosts: tuple[str, ...] = ("chatgpt.com",)
    legacy_result_text: bool = False
    wrappers_file: Path | None = None
    log_level: str = "info"

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"MCP root is not a directory: {root}")
        object.__setattr__(self, "root", root)
        resolved_skill_dirs: list[Path] = []
        for skill_dir in self.skill_dirs:
            resolved_skill_dir = skill_dir.expanduser().resolve()
            if not resolved_skill_dir.is_dir():
                raise ValueError(
                    f"MCP skill directory is not a directory: {resolved_skill_dir}"
                )
            resolved_skill_dirs.append(resolved_skill_dir)
        object.__setattr__(self, "skill_dirs", tuple(resolved_skill_dirs))
        if bool(self.oauth_issuer_url) != bool(self.oauth_authorization_password):
            raise ValueError(
                "oauth_issuer_url and oauth_authorization_password must be configured together"
            )
        if self.oauth_issuer_url:
            issuer = self.oauth_issuer_url.rstrip("/")
            if not issuer.startswith(("https://", "http://localhost")):
                raise ValueError("oauth_issuer_url must use HTTPS")
            object.__setattr__(self, "oauth_issuer_url", issuer)
            state_file = self.oauth_state_file or Path(
                "~/.local/state/yoke-mcp/oauth.json"
            )
            object.__setattr__(
                self, "oauth_state_file", state_file.expanduser().resolve()
            )
        for name in (
            "port",
            "default_yield_ms",
            "python_timeout",
            "max_output_tokens",
            "max_concurrent_calls",
            "max_concurrent_process_starts",
            "max_request_body_size",
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"{name} must be at least 1")

    @property
    def transport_allowed_hosts(self) -> list[str]:
        """Return explicit Host-header values accepted by the MCP transport."""
        defaults = [
            "127.0.0.1",
            "127.0.0.1:*",
            "localhost",
            "localhost:*",
            "[::1]",
            "[::1]:*",
        ]
        return list(dict.fromkeys([*defaults, *self.allowed_hosts]))

    def command_environment(self) -> dict[str, str]:
        """Return the service environment without Yoke MCP control variables."""
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("YOKE_MCP_")
        }


def load_login_shell_environment() -> None:
    """Merge the user's login-shell environment into the MCP service process."""
    if os.name != "posix":
        return
    shell = os.environ.get("SHELL") or "/bin/sh"
    marker = b"__YOKE_LOGIN_ENV_START__\0"
    try:
        completed = subprocess.run(  # noqa: S603
            [shell, "-lc", "printf '__YOKE_LOGIN_ENV_START__\\0'; env -0"],
            check=False,
            capture_output=True,
        )
    except OSError:
        return
    if completed.returncode != 0:
        return
    _, separator, payload = completed.stdout.partition(marker)
    if not separator:
        return
    for entry in payload.split(b"\0"):
        if b"=" not in entry:
            continue
        raw_key, raw_value = entry.split(b"=", 1)
        try:
            key = raw_key.decode()
            value = raw_value.decode()
        except UnicodeDecodeError:
            continue
        if not key or key.startswith("YOKE_MCP_"):
            continue
        os.environ[key] = value


def env_int(name: str, default: int) -> int:
    """Read an integer environment value."""
    value = os.environ.get(name)
    return default if value is None else int(value)


def env_hosts(name: str = "YOKE_MCP_ALLOWED_HOSTS") -> tuple[str, ...]:
    """Read comma-separated transport Host-header patterns."""
    return _split_csv(os.environ.get(name, ""))


def env_paths(name: str) -> tuple[Path, ...]:
    """Read platform-separated filesystem paths from an environment variable."""
    value = os.environ.get(name, "")
    return tuple(Path(item) for item in value.split(os.pathsep) if item.strip())


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
