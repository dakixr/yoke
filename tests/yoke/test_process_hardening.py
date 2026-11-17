"""Cancellation, backpressure and ownership regressions for process tools."""

from __future__ import annotations

from collections.abc import Iterator
import os

import pytest

from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.tools.command_process import (
    input as stdin_delivery,
    spawn,
)


@pytest.fixture
def manager() -> Iterator[CommandProcessManager]:
    manager = CommandProcessManager()
    try:
        yield manager
    finally:
        manager.close()


def test_stdin_retries_short_writes_without_losing_utf8_bytes(monkeypatch):
    read_fd, write_fd = os.pipe()
    delivered = bytearray()

    def short_write(fd, raw):
        assert fd == write_fd
        accepted = min(2, len(raw))
        delivered.extend(raw[:accepted])
        return accepted

    monkeypatch.setattr(stdin_delivery.os, "write", short_write)
    try:
        expected = "hello 🌍\n".encode()
        assert stdin_delivery.write_all(
            write_fd, expected, cancel_requested=None
        ) == len(expected)
        assert bytes(delivered) == expected
        with pytest.raises(OSError):
            os.fstat(write_fd)
    finally:
        os.close(read_fd)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY descriptors")
def test_failed_pty_spawn_closes_both_allocated_descriptors(tmp_path, monkeypatch):
    import pty

    master, slave = pty.openpty()
    monkeypatch.setattr(pty, "openpty", lambda: (master, slave))

    def fail(*args, **kwargs):
        raise FileNotFoundError("injected failed spawn")

    monkeypatch.setattr(spawn.subprocess, "Popen", fail)
    with pytest.raises(FileNotFoundError):
        spawn.open_process(["missing"], tmp_path, os.environ.copy(), tty=True)
    for fd in (master, slave):
        with pytest.raises(OSError):
            os.fstat(fd)
