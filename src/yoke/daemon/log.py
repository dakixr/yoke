"""Bound detached daemon stdio without a log-collecting process."""

from __future__ import annotations

import io
import logging
import sys
from copy import copy, deepcopy
from functools import partial
from collections.abc import Callable, Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import AccessFormatter, DefaultFormatter

DETACHED_ENV = "YOKE_DAEMON_DETACHED"
MAX_LOG_BYTES = 2 * 1024 * 1024


def open_spawn_log(path: Path) -> BinaryIO:
    """Open detached stdio; Windows handles permit the daemon to rotate it."""
    if sys.platform == "win32":
        from yoke.daemon.windows import open_log

        return open_log(path)
    return open(path, "ab")


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


class _SanitizingFormatter(logging.Formatter):
    """Sanitize a daemon-owned rendering without mutating the shared record."""

    def __init__(
        self,
        redact: Callable[[str], str],
        *,
        fmt: str,
        use_colors: bool | None = None,
        access: bool = False,
    ) -> None:
        super().__init__()
        self._redact = redact
        formatter = AccessFormatter if access else DefaultFormatter
        self._formatter = formatter(fmt=fmt, use_colors=use_colors)

    def format(self, record: logging.LogRecord) -> str:
        return self._redact(self._formatter.format(copy(record)))


def daemon_logging_config(redact: Callable[[str], str]) -> dict[str, Any]:
    """Configure only the daemon's root and uvicorn log paths.

    Library applications never call this. No filters are installed on library
    handlers, and no shared LogRecord is rewritten.
    """
    config = deepcopy(LOGGING_CONFIG)
    for name in ("default", "access"):
        config["formatters"][name]["()"] = partial(
            _SanitizingFormatter, redact, access=name == "access"
        )
    config["root"] = {"handlers": ["default"], "level": "WARNING"}
    return config


class _SanitizedStream:
    def __init__(self, stream: TextIO, redact: Callable[[str], str]) -> None:
        self._stream = stream
        self._redact = redact

    def write(self, value: str) -> int:
        self._stream.write(self._redact(value))
        return len(value)

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        return self._stream.isatty()

    def writable(self) -> bool:
        return self._stream.writable()

    def fileno(self) -> int:
        return self._stream.fileno()

    @property
    def encoding(self) -> str:
        return self._stream.encoding


@contextmanager
def sanitized_stdio(redact: Callable[[str], str]) -> Iterator[None]:
    """Cover direct prints to daemon stdio, including the detached log file."""
    with (
        redirect_stdout(_SanitizedStream(sys.stdout, redact)),
        redirect_stderr(_SanitizedStream(sys.stderr, redact)),
    ):
        yield
