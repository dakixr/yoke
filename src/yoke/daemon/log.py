"""Bound detached daemon stdio without a log-collecting process."""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path

DETACHED_ENV = "YOKE_DAEMON_DETACHED"
MAX_LOG_BYTES = 2 * 1024 * 1024


class _LogStream(io.TextIOBase):
    def __init__(self, path: Path) -> None:
        self.handler = RotatingFileHandler(
            path, maxBytes=MAX_LOG_BYTES, backupCount=2, encoding="utf-8"
        )
        self.handler.terminator = ""

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        # Split large single writes too, so one traceback or tool print cannot
        # defeat the size bound. The handler serializes writes from threads.
        for offset in range(0, len(value), 16 * 1024):
            self.handler.handle(
                logging.LogRecord(
                    "daemon.stdio",
                    logging.INFO,
                    "",
                    0,
                    value[offset : offset + 16 * 1024],
                    (),
                    None,
                )
            )
        return len(value)

    def flush(self) -> None:
        self.handler.flush()

    def close(self) -> None:
        super().close()
        self.handler.close()


@contextmanager
def detached_stdio(path: Path) -> Iterator[None]:
    """Keep the current log and two 2 MiB backups, rotating throughout a run."""
    stream = _LogStream(path)
    try:
        with redirect_stdout(stream), redirect_stderr(stream):
            yield
    finally:
        stream.close()
