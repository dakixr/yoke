"""Platform process-tree termination used by managed commands."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from contextlib import suppress

TERM_GRACE_SECONDS = 0.5
KILL_WAIT_SECONDS = 1.0
POLL_SECONDS = 0.01


def terminate_owned_process_tree(
    process: subprocess.Popen[bytes], process_group_id: int
) -> None:
    """Terminate a process tree, including descendants after leader exit."""
    if os.name == "nt":
        _terminate_windows_process_tree(process)
    else:
        _terminate_posix_process_group(process, process_group_id)


def _terminate_posix_process_group(
    process: subprocess.Popen[bytes], process_group_id: int
) -> None:
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        _wait_for_leader(process, timeout=0)
        return

    deadline = time.monotonic() + TERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        process.poll()
        if not _process_group_exists(process_group_id):
            break
        time.sleep(POLL_SECONDS)

    if _process_group_exists(process_group_id):
        with suppress(ProcessLookupError):
            os.killpg(process_group_id, signal.SIGKILL)
    _wait_for_leader(process, timeout=KILL_WAIT_SECONDS)


def _terminate_windows_process_tree(process: subprocess.Popen[bytes]) -> None:
    taskkill = shutil.which("taskkill.exe") or shutil.which("taskkill")
    try:
        result = subprocess.run(  # noqa: S603
            [
                taskkill or "taskkill.exe",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        _kill_process_fallback(process)
    else:
        if result.returncode != 0:
            _kill_process_fallback(process)

    try:
        process.wait(timeout=KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_process_fallback(process)
        process.wait(timeout=KILL_WAIT_SECONDS)


def _kill_process_fallback(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        process.kill()


def _wait_for_leader(process: subprocess.Popen[bytes], *, timeout: float) -> None:
    if process.poll() is not None:
        return
    process.wait(timeout=timeout)


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
