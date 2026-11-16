"""Filesystem locations of the per-user daemon."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8770
# sockaddr_un.sun_path is 108 bytes on Linux, including the terminating NUL.
_MAX_SOCKET_PATH = 107


@dataclass(frozen=True, slots=True)
class DaemonPaths:
    """Where one user's daemon keeps its lock, discovery record, socket and log."""

    state_dir: Path
    run_dir: Path

    @classmethod
    def default(cls) -> DaemonPaths:
        """Resolve ``~/.yoke`` and the runtime directory from the environment."""
        state_dir = Path.home() / ".yoke"
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        run_dir = Path(runtime) / "yoke" if runtime else state_dir / "run"
        if len(os.fsencode(run_dir / "daemon.sock")) > _MAX_SOCKET_PATH:
            run_dir = state_dir / "run"
        return cls(state_dir=state_dir, run_dir=run_dir)

    @property
    def socket(self) -> Path:
        return self.run_dir / "daemon.sock"

    @property
    def lock(self) -> Path:
        return self.state_dir / "daemon.lock"

    @property
    def info(self) -> Path:
        return self.state_dir / "daemon.json"

    @property
    def log(self) -> Path:
        """Detached daemons write stdio here; always under ``~/.yoke/run``."""
        return self.state_dir / "run" / "daemon.log"

    def ensure(self) -> None:
        """Create private directories, refusing a socket path the kernel rejects."""
        for directory in (self.state_dir, self.run_dir, self.log.parent):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
        if len(os.fsencode(self.socket)) > _MAX_SOCKET_PATH:
            raise OSError(f"Daemon socket path is too long: {self.socket}")
