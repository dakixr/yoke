"""Configuration for the Yoke MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_COMMAND_ENV_KEYS = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "PATH",
    "SHELL",
    "TERM",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
)


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
    bearer_token: str | None = None
    log_tool_inputs: bool = False
    log_level: str = "info"

    def __post_init__(self) -> None:
        root = self.root.expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"MCP root is not a directory: {root}")
        object.__setattr__(self, "root", root)
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
        """Build the intentionally small environment inherited by child tools."""
        extra = _split_csv(os.environ.get("YOKE_MCP_COMMAND_ENV_ALLOWLIST", ""))
        keys = (*DEFAULT_COMMAND_ENV_KEYS, *extra)
        return {key: os.environ[key] for key in keys if key in os.environ}


def env_bool(name: str, default: bool = False) -> bool:
    """Read a conventional boolean environment value."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    """Read an integer environment value."""
    value = os.environ.get(name)
    return default if value is None else int(value)


def env_hosts(name: str = "YOKE_MCP_ALLOWED_HOSTS") -> tuple[str, ...]:
    """Read comma-separated transport Host-header patterns."""
    return _split_csv(os.environ.get(name, ""))


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
