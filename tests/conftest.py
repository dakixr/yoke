from __future__ import annotations

import os
import signal
import time
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

from tests.short_paths import short_temp_dir
from tests.yoke.tool_fixture import local_file_tools as local_file_tools

# Resolved at import, before any fixture points HOME at a temporary directory.
# The pwd lookup also ignores a HOME override inherited by the test run.
try:
    import pwd

    _REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)
except ImportError:  # Windows
    _REAL_HOME = Path.home()
from tests.yoke.scheduling import (
    pytest_collection_modifyitems as pytest_collection_modifyitems,
)
from tests.yoke.schema_fixture import (
    empty_schema as empty_schema,
    fresh_database_from_schema as fresh_database_from_schema,
)


@pytest.fixture(scope="session")
def _short_runtime_root() -> Iterator[Path]:
    """Session-lived root under /tmp so per-test runtimes cannot outlive it.

    Cleanup happens after every monkeypatched fixture is undone; removing a
    function-scoped tree can otherwise run while a test still wraps os.scandir.
    """
    with short_temp_dir("yk-rt-") as root:
        yield root


@pytest.fixture(autouse=True)
def isolate_yoke_local_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _short_runtime_root: Path,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("YOKE_STATE_CHECKS", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Ambient DB overrides would make unrelated tests share one database. Tests
    # exercising YOKE_DB_PATH set their own override after this fixture.
    monkeypatch.delenv("YOKE_DB_PATH", raising=False)
    # A real XDG runtime directory is short; tmp_path under macOS's TMPDIR is
    # long enough to push a daemon socket past sun_path.
    runtime = _short_runtime_root / uuid4().hex
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("YOKE_DAEMON_PORT", "0")
    monkeypatch.setenv("YOKE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("YOKE_PROVIDER_LOGS_DIR", str(tmp_path / "provider-logs"))


@pytest.fixture
def short_socket_dir() -> Iterator[Path]:
    """A temp directory whose Unix socket paths fit the smallest sun_path."""
    with short_temp_dir() as directory:
        yield directory


@pytest.fixture(autouse=True, scope="session")
def forbid_real_database_writes() -> Iterator[None]:
    """Fail the run if any test, or a subprocess it starts, creates ~/.yoke/yoke.db."""
    real = _REAL_HOME / ".yoke" / "yoke.db"
    existed = real.exists()
    yield
    if not existed and real.exists():
        pytest.fail(
            f"A test created the real database {real}; isolate HOME/YOKE_DB_PATH."
        )


def _daemon_processes() -> set[int]:
    found: set[int] = set()
    if not Path("/proc").is_dir():
        return found  # Process inspection is Linux-only; other hosts skip the check.
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            arguments = (process / "cmdline").read_bytes().split(b"\0")
            if b"serve" in arguments and b"--daemon" in arguments:
                found.add(int(process.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    return found


@pytest.fixture(autouse=True, scope="session")
def forbid_surviving_test_daemons() -> Iterator[None]:
    """Detect detached test processes too, without touching pre-existing daemons."""
    before = _daemon_processes()
    marker = uuid4().hex
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("YOKE_TEST_RUN_ID", marker)
        yield
    survivors = []
    for pid in _daemon_processes() - before:
        try:
            environment = dict(
                item.split(b"=", 1)
                for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                if b"=" in item
            )
            if environment.get(b"YOKE_TEST_RUN_ID") == marker.encode():
                # Releasing the lifetime lock precedes interpreter teardown.
                # Allow a stopped daemon to finish exiting before failing.
                deadline = time.monotonic() + 5
                command = Path(f"/proc/{pid}/cmdline")
                while command.read_bytes() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if command.read_bytes():
                    survivors.append(pid)
                    os.kill(pid, signal.SIGKILL)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    if survivors:
        pytest.fail(f"Test-spawned yoke serve --daemon survived: {survivors}")
