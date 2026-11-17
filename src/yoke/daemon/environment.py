"""The process environment of an automatically spawned shared daemon."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from yoke.daemon.log import DETACHED_ENV

# Explicit configuration only: never inherit arbitrary YOKE_* credentials,
# attachment policy, or run attribution into a process shared by all clients.
_NAMES = {
    "HOME",
    "USERPROFILE",
    "PATH",
    "LANG",
    "TMPDIR",
    "TEMP",
    "TMP",
    "XDG_RUNTIME_DIR",
    "SYSTEMROOT",
    "COMSPEC",
    "YOKE_DB_PATH",
    "YOKE_SESSION_DIR",
    "YOKE_HTTP_SESSION_DIR",
    "YOKE_DAEMON_PORT",
    "YOKE_DAEMON_IDLE_GRACE",
    "YOKE_DAEMON_BUILD_ID",
    "YOKE_DAEMON_PROTOCOL",
}


def spawn_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Copy daemon configuration and OS basics, removing activated venv paths."""
    result = {
        key: value
        for key, value in values.items()
        if key.upper() in _NAMES or key.upper().startswith("LC_")
    }
    virtual_paths = {
        os.path.normcase(str(Path(prefix) / directory))
        for name in ("VIRTUAL_ENV", "CONDA_PREFIX")
        if (prefix := values.get(name))
        for directory in ("bin", "Scripts")
    }
    if "PATH" in result and virtual_paths:
        result["PATH"] = os.pathsep.join(
            part
            for part in result["PATH"].split(os.pathsep)
            if os.path.normcase(part.rstrip("/\\")) not in virtual_paths
        )
    result[DETACHED_ENV] = "1"
    return result
