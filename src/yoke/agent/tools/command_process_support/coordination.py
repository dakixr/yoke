"""Cooperative locking for process admission and serialized stdin delivery."""

from __future__ import annotations

from _thread import LockType, RLock
from collections.abc import Iterator
from contextlib import contextmanager

from yoke.agent.tools.command_process_types import CancelRequested


class ProcessOperationCancelled(RuntimeError):
    """The caller cancelled before the next process side effect."""


def check_cancelled(cancel_requested: CancelRequested | None) -> None:
    if cancel_requested is not None and cancel_requested():
        raise ProcessOperationCancelled("Process operation cancelled")


@contextmanager
def acquired(
    lock: LockType | RLock, cancel_requested: CancelRequested | None
) -> Iterator[None]:
    """Let queued callers cancel without waiting for the current lock holder."""
    while True:
        check_cancelled(cancel_requested)
        if lock.acquire(timeout=0.05):
            break
    try:
        check_cancelled(cancel_requested)
        yield
    finally:
        lock.release()
