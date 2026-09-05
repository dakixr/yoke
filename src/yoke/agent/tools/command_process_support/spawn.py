"""Low-level subprocess creation for managed command processes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def open_process(
    argv: list[str] | str, cwd: Path, env: dict[str, str], *, tty: bool
) -> tuple[subprocess.Popen[bytes], int | None, int | None]:
    """Open a process in its own platform process group."""
    if tty and os.name != "nt":
        import pty

        master_fd, slave_fd = pty.openpty()
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=cwd,
            env=env,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            start_new_session=True,
            text=False,
        )
        return process, master_fd, slave_fd
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    process = subprocess.Popen(  # noqa: S603
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        text=False,
    )
    return process, None, None
