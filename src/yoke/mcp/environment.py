"""Explicit environment inheritance for MCP subprocesses."""

from __future__ import annotations

from collections.abc import Mapping

import os

from yoke.mcp.config import McpServerConfig

# Runtime essentials only. Credentials and application-specific configuration
# require an explicit env_vars entry or an env value in the server config.
_BASE_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "TMPDIR",
        "TMP",
        "TEMP",
        "TERM",
        "SHELL",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "APPDATA",
        "LOCALAPPDATA",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        # Network plumbing: servers behind a proxy or private CA break without it.
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)


def server_environment(
    server: McpServerConfig, base_environment: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Apply base inheritance, explicit inherited names, then explicit values."""
    source = os.environ if base_environment is None else base_environment
    env = {
        key: value
        for key, value in source.items()
        if key.upper() in _BASE_NAMES or key.startswith("LC_")
    }
    env.update({name: source[name] for name in server.env_vars if name in source})
    env.update(server.env or {})
    return env
